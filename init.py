#!/usr/bin/env python3
"Initialises the database."
import glob

from sqlalchemy import create_engine

import models

if len(glob.glob('db.sqlite3')) > 0:  # Quit if database present.
    exit('Database file db.sqlite3 already exists. Exiting.')

engine = create_engine('sqlite:///db.sqlite3', echo=True)
models.Base.metadata.create_all(engine)
