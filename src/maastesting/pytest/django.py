import datetime
from multiprocessing import Process
import os
import time

from django.db import transaction
import pytest
from requests.exceptions import ConnectionError

from maasapiserver.client import APIServerClient
from maasapiserver.common.db import Database
from maasapiserver.main import run
from maasapiserver.settings import DatabaseConfig
from maasserver.djangosettings import development
from maasserver.testing.resources import close_all_connections
from maasserver.utils.orm import enable_all_database_connections
from maastesting.pytest.database import cluster_stash


@pytest.hookimpl(tryfirst=False)
def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_transactions: Allow a test to use transaction.commit()",
    )
    config.addinivalue_line(
        "markers",
        "recreate_db: re-create database before each test run",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config, parser, args):
    if os.environ.get("DJANGO_SETTINGS_MODULE") is None:
        os.environ[
            "DJANGO_SETTINGS_MODULE"
        ] = "maasserver.djangosettings.development"

    import django
    from django.conf import settings

    database = settings.DATABASES["default"]
    database["NAME"] = "no_such_db"
    django.setup()


@pytest.fixture
def ensuremaasdjangodb(request, ensuremaasdb, pytestconfig, worker_id):
    from maasserver.djangosettings import development

    database = development.DATABASES["default"]
    database["NAME"] = ensuremaasdb
    yield
    database["NAME"] = "no_such_db"


@pytest.fixture
def maasdb(ensuremaasdjangodb, request, pytestconfig):
    enable_all_database_connections()
    # Start a transaction.
    transaction.set_autocommit(False)
    allow_transactions = (
        request.node.get_closest_marker("allow_transactions") is not None
    )
    if allow_transactions:
        yield
        close_all_connections()
        # Since transactions are allowed, we assume a commit has been
        # made, so we can't simply do rollback to clean up the DB.
        dbname = development.DATABASES["default"]["NAME"]
        cluster = pytestconfig.stash[cluster_stash]
        cluster.dropdb(dbname)
    else:
        # Wrap the test in an atomic() block in order to prevent commits.
        with transaction.atomic():
            yield
        # Since we don't allow commits, we can safely rollback and don't
        # have to recreate the DB.
        transaction.rollback()
        close_all_connections()


@pytest.fixture
def maasapiserver(maasdb, tmpdir):
    dbname = development.DATABASES["default"]["NAME"]
    host = development.DATABASES["default"]["HOST"]

    db_config = DatabaseConfig(dbname, host=host)

    os.environ["MAAS_APISERVER_HTTP_SOCKET_PATH"] = os.path.join(
        tmpdir, "maas-apiserver.socket"
    )

    server_process = Process(
        target=lambda: run(Database(db_config)), args=(), daemon=True
    )
    server_process.start()

    timeout = datetime.datetime.utcnow() + datetime.timedelta(seconds=30)
    ready = False

    while not ready and datetime.datetime.utcnow() < timeout:
        try:
            api_client = APIServerClient("")
            root = api_client.get("/")

            if root.status_code == 200:
                ready = True
        except ConnectionError:
            time.sleep(0.1)

    if not ready:
        raise Exception("MaasApiServer did not start within 30 seconds.")

    yield
    server_process.kill()


@pytest.fixture
def factory(maasdb):
    # Local imports from maasserver so that pytest --help works
    from maasserver.testing.factory import factory as maasserver_factory

    return maasserver_factory


@pytest.fixture
def admin(factory):
    return factory.make_admin()


@pytest.fixture
def maas_user(factory):
    return factory.make_User()


@pytest.fixture
def api_client(maas_user):
    # Local imports from maasserver so that pytest --help works
    from maasserver.models.user import get_auth_tokens
    from maasserver.testing.testclient import MAASSensibleOAuthClient

    return MAASSensibleOAuthClient(
        user=maas_user, token=get_auth_tokens(maas_user)[0]
    )


@pytest.fixture
def admin_api_client(admin):
    # Local imports from maasserver so that pytest --help works
    from maasserver.models.user import get_auth_tokens
    from maasserver.testing.testclient import MAASSensibleOAuthClient

    return MAASSensibleOAuthClient(user=admin, token=get_auth_tokens(admin)[0])
