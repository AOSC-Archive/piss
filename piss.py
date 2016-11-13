#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import sched
import sqlite3
import logging
import argparse
import datetime
import collections

import chores

import yaml
import jinja2
import feedgen.feed

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
    cur.execute('PRAGMA journal_mode=WAL')
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
    db.commit()
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

def wrap_fetch(chore, cur):
    updcount = 0
    try:
        for event in chore.fetch():
            cur.execute('INSERT INTO events '
                '(chore, category, time, title, content, url) '
                'VALUES (?,?,?,?,?,?)', event
            )
            updcount += 1
    except Exception:
        logging.exception('Error when fetching %s', chore.name)
        return
    cur.execute(
        'REPLACE INTO chore_status VALUES (?,?,?)',
        (chore.name, chore.status.updated, chore.status.last_result)
    )
    logging.info('%s got %d updates.', chore.name, updcount)

def run_update(args):
    logging.info('Getting updates...')
    db = init_db(args.db)
    cur = db.cursor()
    cfg = next(yaml.safe_load_all(open(args.chores, 'r', encoding='utf-8')))
    chores_avail = []
    tasks = sched.scheduler(time.time)
    for name, config in cfg.items():
        result = cur.execute('SELECT updated, last_result FROM chore_status WHERE name = ?', (name,)).fetchone()
        if result:
            result = chores.ChoreStatus(*result)
        chorename = config.pop('chore')
        chore = chores.CHORE_HANDLERS[chorename](name, status=result, **config)
        chores_avail.append((chorename, chore))
    try:
        while 1:
            for chorename, chore in chores_avail:
                tasks.enterabs(
                    chore.status.updated + args.keep,
                    chores.CHORE_PRIO[chorename],
                    wrap_fetch, (chore, cur)
                )
            tasks.run()
            db.commit()
            if args.keep:
                logging.info('A round of updating completed.')
            else:
                break
    except KeyboardInterrupt:
        logging.warning('Interrupted.')
    finally:
        db.commit()

def get_events(cur, limit=None):
    if limit:
        cur.execute('SELECT * FROM events ORDER BY time DESC LIMIT ?', (limit,))
    else:
        cur.execute('SELECT * FROM events ORDER BY time DESC')
    row = cur.fetchone()
    while row:
        yield row[0], chores.Event(*row[1:])
        row = cur.fetchone()

def format_events(events, out_format, **kwargs):
    txttime = lambda timestamp: time.strftime('%Y-%m-%d %H:%M', time.localtime(timestamp))
    if out_format in ('term', 'text'):
        lines = []
        for k, news in events:
            if out_format == 'term':
                lines.append("%s \x1b[1;32m%s \x1b[1;39m%s\x1b[0m \t%s" %
                    (txttime(news.time), news.category or '', news.chore, news.title))
            else:
                lines.append("%s %s %s\t%s" %
                    (txttime(news.time), news.category or '', news.chore, news.title))
        lines.reverse()
        return '\n'.join(lines) + '\n'
    elif out_format == 'atom':
        fg = feedgen.feed.FeedGenerator()
        fg.title(kwargs['title'])
        fg.subtitle(kwargs['subtitle'])
        fg.id(kwargs['id'])
        if kwargs.get('link'):
            fg.link(href=kwargs['link'], rel='alternate')
        fg.language('en')
        for k, news in events:
            fe = fg.add_entry()
            fe.id(kwargs['id'] + '/' + str(k))
            fe.title('%s: %s' % (news.chore, news.title))
            fe.category({'term': news.category or 'unclassified'})
            fe.published(datetime.datetime.fromtimestamp(news.time, datetime.timezone.utc))
            fe.author({'name': news.chore})
            fe.content(news.content, None, 'html')
            fe.link({'href': news.url, 'rel': 'alternate'})
        return fg.atom_str(pretty=True).decode('utf-8')
    elif out_format == 'jinja2':
        jinjaenv = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))
        jinjaenv.filters['strftime'] = (
            lambda t, f='%Y-%m-%dT%H:%M:%SZ': time.strftime(f, t))
        template = jinjaenv.get_template(kwargs['template'])
        kvars = kwargs.copy()
        kvars['events'] = []
        for k, news in events:
            d = news._asdict()
            d['id'] = k
            d['time'] = time.gmtime(news.time)
            kvars['events'].append(d)
        return template.render(**kvars)
    else:
        raise ValueError('unsupported output format: %s' % out_format)

def generate_feed(args):
    logging.info('Reading database...')
    db = init_db(args.db)
    cur = db.cursor()
    if args.output == '-':
        f = sys.stdout
    else:
        f = open(args.output, 'w', encoding='utf-8')
    f.write(format_events(get_events(cur, args.number), args.format, **vars(args)))
    f.close()

def main():
    parser = argparse.ArgumentParser(description='Projects Information Storage System')
    subparsers = parser.add_subparsers()
    parser_gen = subparsers.add_parser('generate', help='Try to generate a chores config file from abbs-meta database or browser bookmarks.')
    parser_gen.add_argument('-d', '--db', help='abbs-meta database file')
    parser_gen.add_argument('-b', '--bookmark', help='browser exported HTML bookmark file')
    parser_gen.add_argument('-e', '--existing', help='base on this existing config file')
    parser_gen.add_argument('output', nargs='?', default='chores.yaml', help='output YAML file')
    parser_gen.set_defaults(func=generate_config)
    parser_cron = subparsers.add_parser('run', help='Get updates.')
    parser_cron.add_argument('-d', '--db', default='piss.db', help='piss database file')
    parser_cron.add_argument('-c', '--chores', default='chores.yaml', help='chores YAML config file')
    parser_cron.add_argument('-k', '--keep', type=int, metavar='INTERVAL', default=0, help='keep running, check updates every INTERVAL minutes')
    parser_cron.set_defaults(func=run_update)
    parser_serve = subparsers.add_parser('check', help='Check out the latest news.')
    parser_serve.add_argument('-d', '--db', default='piss.db', help='piss database file')
    parser_serve.add_argument('-f', '--format', choices=('term', 'text', 'atom', 'jinja2'), default='term', help='output format')
    parser_serve.add_argument('-T', '--template', help='template file')
    parser_serve.add_argument('-t', '--title', default='PISS Updates', help='news feed title')
    parser_serve.add_argument('-s', '--subtitle', default='New packaging tasks', help='news feed subtitle')
    parser_serve.add_argument('-i', '--id', default='pissnews', help='id for feed formats')
    parser_serve.add_argument('-l', '--link', help='url for PISS website')
    parser_serve.add_argument('-L', '--lang', default='en', help='language setting for feed formats')
    parser_serve.add_argument('-n', '--number', type=int, metavar='NUM', default=100, help='limit max number of events (default: 100, all: 0)')
    parser_serve.add_argument('output', nargs='?', default='-', help='output feed')
    parser_serve.set_defaults(func=generate_feed)
    args = parser.parse_args()
    args.func(args)
    return 0

if __name__ == '__main__':
    sys.exit(main())
