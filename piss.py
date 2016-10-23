#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import sqlite3
import logging

import upstreamchecker

logging.basicConfig(
    format='%(asctime)s %(levelname).1s %(message)s', level=logging.INFO)

formattime = lambda timestamp: time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))

db = sqlite3.connect(sys.argv[1])
cur = db.cursor()

def init_db(cur):
    cur.execute('CREATE TABLE IF NOT EXISTS package_version ('
        'package TEXT,'
        'source TEXT,' # upstream, abbs, deb
        'arch TEXT,'
        'version TEXT,'
        'FOREIGN KEY(package) REFERENCES packages(name)'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS upstreams ('
        'name TEXT PRIMARY KEY,'
        'type TEXT,'
        'url TEXT,'
        'branch TEXT,'
        'FOREIGN KEY(package) REFERENCES packages(name)'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS package_upstream ('
        'package TEXT PRIMARY KEY,'
        'upstream TEXT,'
        'FOREIGN KEY(package) REFERENCES packages(name),'
        'FOREIGN KEY(upstream) REFERENCES upstreams(name)'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS upstream_update ('
        'upstream TEXT,'
        'category TEXT,' # commit, issue, pr, tag, release, news
        'time INTEGER,'
        'subscription INTEGER,'
        'title TEXT,'
        'content TEXT,'
        'url TEXT UNIQUE,'
        'FOREIGN KEY(upstream) REFERENCES upstreams(name),'
        'FOREIGN KEY(subscription) REFERENCES upstream_subscription(id)'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS upstream_subscription ('
        'id INTEGER PRIMARY KEY,'
        'upstream TEXT,'
        'type TEXT,' # feed, email
        'category TEXT,' # all, upstream_update.category
        'url TEXT,'
        'last_update INTEGER,'
        'FOREIGN KEY(upstream) REFERENCES upstreams(name)'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS pakreq ('
        'package TEXT PRIMARY KEY,'
        'description TEXT,'
        'url TEXT,'
        'resolution TEXT'
    ')')

def upstream_import(cur):
    srcs = cur.execute("SELECT DISTINCT package, value FROM package_spec WHERE key like '%SRC%' AND package NOT IN (SELECT package FROM upstream_subscription)").fetchall()

print('Detecting upstreams...')
upstreams = list(filter(None, (upstreamchecker.detect_upstream(name, url) for name, url in srcs)))
updates = []

print('Getting updates...')

try:
    for u in upstreams:
        updates.extend(u.get_updates())
        #print(u.name)
        #upd = u.get_updates()
        #upd.sort(key=lambda x: x.time)
except KeyboardInterrupt:
    pass

updates.sort(key=lambda x: x.time)

for news in updates[-10:]:
    print("%s \x1b[1;32m%s \x1b[1;39m%s\x1b[0m \t%s" % (formattime(news.time), news.category, news.upstream, news.title))
