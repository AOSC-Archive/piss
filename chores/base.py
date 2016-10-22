"""Base."""


class Chore(object):
    """Base class of Chore for others to inherit.
    """

    def __init__(self, identifier, name, params, last_result=None):
        """Initialises a Chore.

        Args:
            identifier (int): Chore ID in database.
            name (str): User-friendly name.
            params (dict): Chore-specific parameters.
            last_result (str): Latest result (ex. dates) to avoid duplicate entries.
        """
        self.name = name
        self.identifier = identifier
        self.last_result = last_result

    def _construct_message(self, message, continue_url):
        """Constructs event messages.

        Arguments and return values should be customised during inheritance according to needs.

        Args:
            message (str): Original message.
            continue_url (str): URL for continued reading.

        Returns:
            tuple: Tuple of formatted message and continue URL.
        """
        return ('{name}: {message}'.format(name=self.name,
                                           message=message), continue_url)

    def fetch(self):
        """Seeks the upstream for update.

        last_result should be updated when there are new events.

        Returns:
            list: List of tuples of (message, continue_url)
        """
        return [self._construct_message('', 'http://invalid')]

    def __repr__(self):
        return '{chore_type} ({name})'.format(chore_type=type(self).__name__,
                                              name=self.name)
