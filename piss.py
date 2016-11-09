#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time
import sqlite3
import logging
import argparse
import collections

import chores

import yaml

logging.basicConfig(
    format='%(asctime)s %(levelname).1s %(message)s', level=logging.DEBUG)
logging.captureWarnings(True)
logging.getLogger().handlers[0].addFilter(logging.Filter('root'))

def setup_yaml():
    """ http://stackoverflow.com/a/8661021 """
    represent_dict_order = lambda self, data: self.represent_mapping(
        'tag:yaml.org,2002:map', data.items())
    yaml.add_representer(collections.OrderedDict, represent_dict_order)  

def init_db(filename):
    db = sqlite3.connect(filename)
    cur = db.cursor()
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
    return db

def generate_config(args):
    setup_yaml()
    if args.existing:
        existing = next(yaml.safe_load_all(open(args.existing, 'r', encoding='utf-8')))
    else:
        existing = None
    cfg, failed = chores.generate_chore_config(args.db, args.bookmark, existing)
    with open(args.output, 'w', encoding='utf-8') as f:
        yaml.dump_all((cfg, failed), f, default_flow_style=False)
    logging.info('Done.')

def run_update(args):
    logging.info('Getting updates...')
    ...

def generate_feed(args):
    formattime = lambda timestamp: time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))
    # updates.sort(key=lambda x: x.time)
    # for news in updates[-10:]:
        # print("%s \x1b[1;32m%s \x1b[1;39m%s\x1b[0m \t%s" % (formattime(news.time), news.category, news.upstream, news.title))
    ...

def main():
    parser = argparse.ArgumentParser(description='Projects Information Storage System')
    subparsers = parser.add_subparsers()
    parser_gen = subparsers.add_parser('generate', help='Try to generate a chores config file from abbs-meta database or browser bookmarks.')
    parser_gen.add_argument('-d', '--db', help='abbs-meta database file')
    parser_gen.add_argument('-b', '--bookmark', help='browser exported HTML bookmark file')
    parser_gen.add_argument('-e', '--existing', help='base on this existing config file')
    parser_gen.add_argument('output', nargs='?', default='chores.yaml', help='output YAML file')
    parser_gen.set_defaults(func=generate_config)
    parser_cron = subparsers.add_parser('update', help='Get updates.')
    parser_cron.add_argument('-k', '--keep', help='keep running')
    parser_cron.set_defaults(func=run_update)
    parser_serve = subparsers.add_parser('check', help='Check out the latest news.')
    parser_serve.add_argument('-f', '--format', choices=('term', 'text', 'atom', 'rss'), default='term', help='output format')
    parser_serve.add_argument('output', help='output feed')
    parser_serve.set_defaults(func=generate_feed)
    args = parser.parse_args()
    args.func(args)
    return 0

if __name__ == '__main__':
    sys.exit(main())
