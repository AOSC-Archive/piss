#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import sqlite3
import logging

import yaml

import chores

logging.basicConfig(
    format='%(asctime)s %(levelname).1s %(message)s', level=logging.INFO)
logging.captureWarnings(True)

formattime = lambda timestamp: time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))

db = sqlite3.connect(sys.argv[1])
cur = db.cursor()

def init_db(cur):
    cur.execute('CREATE TABLE IF NOT EXISTS chore_status ('
        'name TEXT PRIMARY KEY,'
        'updated INTEGER,'
        'last_result TEXT'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS events ('
        'id INTEGER PRIMARY KEY,'
        'chore TEXT,'
        'category TEXT,' # commit, issue, pr, tag, release, news
        'time INTEGER,'
        'title TEXT,'
        'content TEXT,'
        'url TEXT'
    ')')

def auto_generate_config(abbs_db, bookmarks_html):
    db_abbs = sqlite3.connect(abbs_db)
    cur_abbs = db.cursor()
    srcs = cur_abbs.execute("SELECT DISTINCT package, value FROM package_spec WHERE key like '%SRC%' AND package NOT IN (SELECT package FROM upstream_subscription)").fetchall()

    print('Detecting upstreams...')
    upstreams = list(filter(None, (chores.detect_upstream(name, url) for name, url in srcs)))

def run_update():
    print('Getting updates...')
    updates = []
    try:
        for u in upstreams:
            updates.extend(u.get_updates())
    except KeyboardInterrupt:
        pass

def format_events(events):
    updates.sort(key=lambda x: x.time)
    for news in updates[-10:]:
        print("%s \x1b[1;32m%s \x1b[1;39m%s\x1b[0m \t%s" % (formattime(news.time), news.category, news.upstream, news.title))

def generate_feed(cur, output):
    ...

