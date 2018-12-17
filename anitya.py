#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import sqlite3
import argparse

import requests

API_ENDPOINT = os.environ.get('API_ENDPOINT', 'https://release-monitoring.org/api/v2/')
RE_VER_PREFIX = re.compile(r'^(?:version|ver|v|releases|release|rel|r)[._/-]?', re.I)
RE_VERSION_UNDERLINE = re.compile(r"(\d+)_(\d+)")

re_projectrep = re.compile(r'^[^/]+/|[. _-]')

ecosystems = {
    "pypi": "PyPI",
    "npmjs": "npm",
    "Rubygems": "rubygems",
    "Maven Central": "maven",
    "PyPI": "PyPI",
    "crates.io": "crates.io",
}

cmp = lambda a, b: ((a > b) - (a < b))

backend_cmp = lambda a, b: cmp(ecosystems.get(a, ''), ecosystems.get(b, ''))

def version_underline_norm(version):
    if not RE_VERSION_UNDERLINE.match(version):
        return version
    while RE_VERSION_UNDERLINE.search(version):
        version = RE_VERSION_UNDERLINE.sub(r'\1.\2', version)
    return version

def anitya_api(method, **params):
    req = requests.get(API_ENDPOINT + method, params=params, timeout=300)
    req.raise_for_status()
    return json.loads(req.content.decode('utf-8'))

def init_db(db):
    cur = db.cursor()
    updated = cur.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND tbl_name='anitya_projects' "
        "AND sql LIKE '%ecosystem%'").fetchone()
    if not updated:
        cur.execute('DROP TABLE IF EXISTS anitya_projects')
    cur.execute('CREATE TABLE IF NOT EXISTS anitya_projects ('
                'id INTEGER PRIMARY KEY,'
                'name TEXT,'
                'homepage TEXT,'
                'ecosystem TEXT,'
                'backend TEXT,'
                'version_url TEXT,'
                'regex TEXT,'
                'latest_version TEXT,'
                'updated_on INTEGER,'
                'created_on INTEGER'
                ')')
    cur.execute('CREATE TABLE IF NOT EXISTS anitya_link ('
                'package TEXT PRIMARY KEY,'
                'projectid INTEGER'
                ')')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_anitya_projects'
                ' ON anitya_projects (name)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_anitya_link'
                ' ON anitya_link (projectid)')
    db.commit()

def check_update(db):
    #if not anitya_api('version')['version'].startswith('1.'):
        #raise ValueError('anitya API version not supported')
    total_items = 1
    got_items = 0
    page = 1
    retrys = 0
    cur = db.cursor()
    while got_items < total_items and retrys < 5:
        try:
            projects = anitya_api('projects', page=page, items_per_page=250)
        except Exception:
            retrys += 1
            continue
        total_items = projects['total_items']
        for project in projects['items']:
            got_items += 1
            if project['version']:
                ver = re.sub('^' + re.escape(project['name']) + '[._-]', '',
                             project['version'], flags=re.I)
                ver = version_underline_norm(RE_VER_PREFIX.sub('', ver))
            else:
                ver = None
            cur.execute('REPLACE INTO anitya_projects VALUES (?,?,?,?,?,?,?,?,?,?)', (
                project['id'], project['name'], project['homepage'],
                project['ecosystem'], project['backend'],
                project['version_url'], project['regex'], ver,
                int(project['updated_on']), int(project['created_on'])
            ))
        page += 1
        db.commit()

def detect_links(db, abbsdbfile):
    cur = db.cursor()
    projects = cur.execute(
        'SELECT id, name FROM anitya_projects ap '
        'INNER JOIN ( '
        '  SELECT name, min(backend COLLATE backend_cmp) backend '
        '  FROM anitya_projects GROUP BY name '
        ') t1 USING (name, backend) ORDER BY id').fetchall()
    project_index = {}
    for row in projects:
        name_index = re_projectrep.sub('', row[1].lower())
        if name_index not in project_index:
            project_index[name_index] = row
    links = {}
    abbsdb = sqlite3.connect(abbsdbfile)
    for row in abbsdb.execute('SELECT name FROM packages ORDER BY name'):
        name = row[0]
        name_index = name.lower().replace('-', '').replace(' ', '').replace('_', '')
        if name_index in project_index:
            links[name] = project_index[name_index]
    for k, v in links.items():
        cur.execute('REPLACE INTO anitya_link VALUES (?,?)', (k, v[0]))
    db.commit()

def update_db(database, abbsdbfile, reset=False):
    db = sqlite3.connect(database)
    db.create_collation("backend_cmp", backend_cmp)
    cur = db.cursor()
    if reset:
        cur.execute('DROP TABLE IF EXISTS anitya_projects')
        cur.execute('DROP TABLE IF EXISTS anitya_link')
    init_db(db)
    check_update(db)
    detect_links(db, abbsdbfile)
    cur.execute('PRAGMA optimize')
    cur.execute('VACUUM')
    db.commit()

def main():
    parser = argparse.ArgumentParser(description="Store and process project versions from Anitya.")
    parser.add_argument("-d", "--detect", help="Auto detect links", action='store_true')
    parser.add_argument("--reset", help="Reset database", action='store_true')
    parser.add_argument("abbsdb", help="Abbs database file (abbs.db)")
    parser.add_argument("database", help="PISS database file")
    args = parser.parse_args()

    update_db(args.database, args.abbsdb, args.reset)
    print('Done.')

if __name__ == '__main__':
    main()
