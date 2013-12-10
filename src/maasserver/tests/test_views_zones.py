# Copyright 2013 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver zones views."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []


from django.core.urlresolvers import reverse
from maasserver.testing import get_content_links
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    AdminLoggedInTestCase,
    LoggedInTestCase,
    )
from maastesting.matchers import ContainsAll
from testtools.matchers import (
    Equals,
    MatchesAll,
    Not,
    )


class ZoneListingViewTest(LoggedInTestCase):

    def test_zone_list_link_present_on_homepage(self):
        response = self.client.get(reverse('index'))
        zone_list_link = reverse('zone-list')
        self.assertIn(
            zone_list_link,
            get_content_links(response, element='#main-nav'))

    def test_zone_list_displays_zone_details(self):
        # Zone listing displays the zone name and the zone description.
        zones = [factory.make_zone() for i in range(3)]
        response = self.client.get(reverse('zone-list'))
        zone_names = [zone.name for zone in zones]
        truncated_zone_descriptions = [
            zone.description[:20] for zone in zones]
        self.assertThat(response.content, ContainsAll(zone_names))
        self.assertThat(
            response.content, ContainsAll(truncated_zone_descriptions))

    def test_zone_list_displays_sorted_list_of_zones(self):
        # Zones are alphabetically sorted on the zone list page.
        zones = [factory.make_zone() for i in range(3)]
        sorted_zones = sorted(zones, key=lambda x: x.name.lower())
        response = self.client.get(reverse('zone-list'))
        zone_links = [
            reverse('zone-view', args=[zone.name])
            for zone in sorted_zones]
        self.assertEqual(
            zone_links,
            [link for link in get_content_links(response)
                if link.startswith('/zones/')])


class ZoneListingViewTestNonAdmin(LoggedInTestCase):

    def test_zone_list_does_not_contain_edit_and_delete_links(self):
        zones = [factory.make_zone() for i in range(3)]
        response = self.client.get(reverse('zone-list'))
        zone_edit_links = [
            reverse('zone-edit', args=[zone.name]) for zone in zones]
        zone_delete_links = [
            reverse('zone-del', args=[zone.name]) for zone in zones]
        all_links = get_content_links(response)
        self.assertThat(
            all_links,
            MatchesAll(*[Not(Equals(link)) for link in zone_edit_links]))
        self.assertThat(
            all_links,
            MatchesAll(*[Not(Equals(link)) for link in zone_delete_links]))

    def test_zone_list_does_not_contain_add_link(self):
        response = self.client.get(reverse('zone-list'))
        add_link = reverse('zone-add')
        self.assertNotIn(add_link, get_content_links(response))


class ZoneListingViewTestAdmin(AdminLoggedInTestCase):

    def test_zone_list_contains_edit_links(self):
        zones = [factory.make_zone() for i in range(3)]
        response = self.client.get(reverse('zone-list'))
        zone_edit_links = [
            reverse('zone-edit', args=[zone.name]) for zone in zones]
        zone_delete_links = [
            reverse('zone-del', args=[zone.name]) for zone in zones]
        all_links = get_content_links(response)
        self.assertThat(all_links, ContainsAll(zone_edit_links))
        self.assertThat(all_links, ContainsAll(zone_delete_links))

    def test_zone_list_contains_add_link(self):
        response = self.client.get(reverse('zone-list'))
        add_link = reverse('zone-add')
        self.assertIn(add_link, get_content_links(response))
