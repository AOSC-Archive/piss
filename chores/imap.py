"""Contains IMAPChore for email parsing."""
import os

from uuid import uuid4
import json
import requests
import imaplib
import email
import re

from .base import Chore


class IMAPInfoNotCompleteException(Exception):
    """Raised when not all of four IMAP parameters are set."""
    pass


class GistHTTPErrorException(Exception):
    """Raised when GitHub returns an error code."""
    pass


def paste_to_gist(text):
    """Pastes text to GitHub Gist.

    Arguments:
        text (str): Content of the Gist file.

    Returns:
        str: Generated browser-friendly URL.
    """
    r = requests.post(
        'https://api.github.com/gists',
        data=json.dumps(
            {'files': {'{uuid}.txt'.format(uuid=uuid4()): {'content': text}}}))
    if r.status_code != 201:
        raise GistHTTPErrorException('{status}: {text}'.format(
            status=r.status_code, text=r.text))
    return json.loads(r.text)['html_url']


class IMAPChore(Chore):
    """Chore for parsing emails from an IMAP mailbox."""

    def __init__(self, identifier, name, params, last_result=None):
        """Initialises IMAPChore.

            Args:
                identifier (int): Chore ID in database.
                name (str): User-friendly name.
                params (dict): Params (see below) specific to IMAPChore.
                last_result (str): Not used - processed emails will be removed.

            Chore-specific params:
                subject_regex (str): Subject must match expression to be included.
                from_regex (str): Sender much match expression to be included.
                body_regex (str): Body must match express to be included.
                imap_host (str): DNS name or IP address of IMAP server.
                imap_username (str): Username of IMAP mailbox.
                imap_password (str): Password of IMAP mailbox.
                imap_folder (str): Path name of IMAP mailbox.

                If an regex parameter is empty, anything will match.
                IMAP parameters above will override system variables of the same names.
        """
        self.identifier = identifier
        self.name = name
        self.params = params
        self.last_result = None  # Not used - see docstring.

        self.subject_regex = params.setdefault('subject_regex', '.*')
        self.from_regex = params.setdefault('from_regex', '.*')
        self.body_regex = params.setdefault('body_regex', '.*')
        self.imap_host = params.setdefault('imap_host',
                                           os.environ.get('imap_host'))
        self.imap_username = params.setdefault('imap_username',
                                               os.environ.get('imap_username'))
        self.imap_password = params.setdefault('imap_password',
                                               os.environ.get('imap_password'))
        self.imap_folder = params.setdefault('imap_folder',
                                             os.environ.get('imap_folder'))

        self.MESSAGE_FORMAT = '{subject} by {from_identity}'  # Event message format.

        if not self.imap_host or not self.imap_username or not self.imap_password or not self.imap_folder:
            raise IMAPInfoNotCompleteException()

    def validate_message(self, subject, from_identity, body):
        """Performs regex checks to see if message matches criteria.

        Args:
            subject (str): Subject of the e-mail message.
            from_identity (str): From (sender) field of the message.
            body (str): Plain-text body of the message.

        Returns:
            bool: True if the message passes all regex checks, False if not.
        """
        if re.match(self.subject_regex, subject) and re.match(
                self.from_regex, from_identity) and re.match(self.body_regex,
                                                             body):
            return True
        return False

    def fetch(self):
        """Iterates all emails in a mailbox, filters matched ones, and turns them
            into events
        """
        # Sign-in to mailbox
        imap = imaplib.IMAP4_SSL(self.imap_host)
        imap.login(self.imap_username, self.imap_password)
        imap.select(self.imap_folder)

        # Filter emails from IMAP mailbox.
        filtered_emails = []

        _, data = imap.search(None, 'ALL')
        for seq in data[0].split():
            _, data = imap.fetch(seq, '(RFC822)')
            message = email.message_from_bytes(data[0][1])
            subject = email.header.decode_header(message['Subject'])[0][0]
            from_identity = email.utils.formataddr(email.utils.parseaddr(
                email.header.decode_header(message['From'])[0][0]))

            body_parts = []
            for part in message.walk():
                if part.get_content_type() == 'text/plain':
                    body_parts.append(part.get_payload(decode=True))
                    break
            if not body_parts:
                [body_parts.append(part.get_payload(decode=True))
                 for part in message.walk()]
            body = '\n'.join([part.decode("utf-8") for part in body_parts])

            if self.validate_message(subject, from_identity, body):
                imap.store(seq, '+FLAGS',
                           r'(\Deleted)')  # Delete proceesed email
                filtered_emails.append((subject, from_identity, body))

        imap.expunge()
        imap.close()
        imap.logout()

        # Prepare tuples for returning
        events = []
        for mail in filtered_emails:
            continue_url = paste_to_gist(mail[2])
            events.append(self._construct_message(
                self.MESSAGE_FORMAT.format(subject=mail[0],
                                           from_identity=mail[1]),
                continue_url))

        return events
