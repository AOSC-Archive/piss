"""SQLAlchemy models."""
import datetime

from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship

Base = declarative_base()


class Project(Base):
    """Software project of concern.

    Usually, a Project should have its own release page or repository, and is
    not related to how distros organize and separate their packages.

    Attrs:
            identifier (Column(Integer)): Unique ID within the table.
            name (Column(String)): Official unstylised name of the project.
            description (Column(String)): Concise description)ex. purpose).
            upstream_url (Column(String)): Official site or repository.

            events(relationship): Event entries associated.
            Chores(relationship): Chore entries associated.
    """
    __tablename__ = 'project'

    identifier = Column(Integer, primary_key=True)
    name = Column(String(50))
    description = Column(String)
    upstream_url = Column(String(500))

    events = relationship('Event', backref='project')
    chores = relationship('Chore', backref='project')


class Event(Base):
    """News event of a Project.

    Attrs:
        identifier (Column(Integer)): Unique ID within the table.
        message (Column(String)): Content of the event message.
        timestamp (Column(DateTime)): Timestamp of addition.
        continue_url (Column(String)): URL for continued reading.

        project_id (Column(Integer)): The Project a Event is associated with.
    """
    __tablename__ = 'event'

    identifier = Column(Integer, primary_key=True)
    message = Column(String)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    continue_url = Column(String(500))

    project_id = Column(Integer, ForeignKey('project.identifier'))


class Chore(Base):
    """Scheduled job that fetch Event entries.

    Attrs:
        identifier (Column(Integer)): Unique ID within the table.
        name (Column(String)): User-friendly name of the Chore.
        chore_type (Column(String)): Type of available Chore to be run.
        parameters (Column(String)): JSON-dumped variables passed as dict onto Chore objects.
        last_successful_run (Column(DateTime)): Last time the Chore was run without errors.
        last_result (Column(String)): Identifying parameter to avoid duplicate entries.

    """
    __tablename__ = 'chore'

    identifier = Column(Integer, primary_key=True)
    name = Column(String(50))
    chore_type = Column(String(30))
    parameters = Column(String, default='{}')
    last_successful_run = Column(DateTime,
                                 default=datetime.datetime(1970, 1, 1, 0, 0))
    last_result = Column(String, default='')

    project_id = Column(Integer, ForeignKey('project.identifier'))
