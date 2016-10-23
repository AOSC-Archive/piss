"""Contains FeedChore for feed parsing."""
import feedparser

from .base import Chore


class FeedChore(Chore):
    """Chore for fetching and parsing RSS and Atom feeds.
    """

    def __init__(self, identifier, name, params, last_result=None):
        """Initialises FeedChore.

        Args:
            identifier (int): Chore ID in database.
            name (str): User-friendly name.
            params (dict): Params (see below) specific to FeedChore.
            last_result (str): Latest title to avoid duplicates.

        Chore-specific params:
            feed_url (str): HTTP URL of the RSS/Atom feed.
            result_element (str): Element of the feed entry to appear in the message.
        """
        self.identifier = identifier
        self.name = name
        self.feed_url = params.setdefault('feed_url', '')
        self.result_element = params.setdefault('result_element', 'title')
        self.last_result = last_result

    def fetch(self):
        """Fetches feed and filter out new entries using feedparser.
        """
        last_result = self.last_result
        messages = []  # Event messages to be added to this list.

        feed = feedparser.parse(self.feed_url)
        for counter, entry in enumerate(feed.entries):
            if entry.title == last_result:  # Stop fetching where last run ended.
                break
            elif counter == 0:  # First entry is usually the latest.
                self.last_result = entry.title

            messages.append(self._construct_message(entry.title, entry.link))

        return messages
