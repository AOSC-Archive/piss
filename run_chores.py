#!/usr/bin/env python3
"""Executes Chores in database and adds Events into database"""
import glob
import json
import datetime
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Event, Chore
from chores.feed import FeedChore
from chores.imap import IMAPChore

CHORE_MAP = {'FeedChore': FeedChore, 'IMAPChore': IMAPChore}


def construct_event(project_id, message, continue_url):
    """Constructs Event SQLAlchemy instance.

    Arguments after project_id are usually unpacked from tuples.

    Args:
        project_id (int): Project associated with the event.
        message (str): Event message.
        continue_url (str): URL for continued reading.

    Returns:
        Event
    """
    return Event(project_id=project_id,
                 message=message,
                 continue_url=continue_url)


if __name__ == '__main__':
    if len(glob.glob('db.sqlite3')) == 0:  # Checks if database is present.
        exit('Database file db.sqlite3 not found. Consider running init.py.')

    engine = create_engine('sqlite:///db.sqlite3')
    Base.metadata.bind = engine
    Session = sessionmaker(bind=engine)
    session = Session()

    chore_entries = session.query(Chore).all()

    for entry in chore_entries:
        print('Conducting #{chore_id}: {name} for Project #{project_id}'.format(
            chore_id=entry.identifier,
            name=entry.name,
            project_id=entry.project_id))

        try:
            chore = CHORE_MAP[entry.chore_type](
                entry.identifier, entry.name, json.loads(entry.parameters),
                entry.last_result)  # Creates Chore instance with type name.
        except KeyError:
            print('----- ERROR: Invalid Chore type in database. Skipping.')
            continue

        print('----- Fetching.')
        try:
            for result in chore.fetch():
                event = construct_event(entry.project_id, *result)
                print('----- New event: {message}'.format(
                    message=event.message))
                session.add(event)
        except:
            print('----- ERROR: {e}. Skipping.'.format(e=sys.exc_info()[0]))
            continue

        session.query(Chore).filter_by(identifier=entry.identifier).update(
            {'last_result': chore.last_result,
             'last_successful_run': datetime.datetime.utcnow()})
        session.commit()
        print('----- Done.')

    session.close()
