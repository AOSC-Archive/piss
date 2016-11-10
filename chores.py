#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import difflib
import logging
import sqlite3
import calendar
import warnings
import collections
import urllib.parse

import bs4
import ftputil
import requests
import feedparser

__version__ = '0.1'

USER_AGENT = 'Mozilla/5.0 (compatible; PISS/%s; +https://github.com/AOSC-Dev/piss)' % __version__

RE_FEED = re.compile("atom|rss|feed", re.I)
RE_GITHUB = re.compile("github.com", re.I)

ChoreType = collections.namedtuple('ChoreType', ('name', 'chore', 'kwargs'))
ChoreStatus = collections.namedtuple('ChoreStatus', ('updated', 'last_result'))

STATUS_NONE = ChoreStatus(0, None)

Event = collections.namedtuple('Event', (
    'chore',    # Chore name
    'category', # Category: commit, issue, pr, tag, release, news
    'time',     # Original unix timestamp of the event message.
    'title',    # Title of the event message.
    'content',  # Content of the event message.
    'url'       # URL for continued reading.
))

HSESSION = requests.Session()
HSESSION.headers['User-Agent'] = USER_AGENT

def uniq(seq, key=None): # Dave Kirby
    # Order preserving
    seen = set()
    if key:
        return [x for x in seq if key(x) not in seen and not seen.add(key(x))]
    else:
        return [x for x in seq if x not in seen and not seen.add(x)]

class ExtendedChoreStatus(ChoreStatus):
    def load(self):
        return json.loads(self.last_result or '{}')

    def save(self, d):
        return ExtendedChoreStatus(int(time.time()), json.dumps(d))

class Chore:
    def __init__(self, name, status=None, **kwargs):
        self.name = name
        # should update db after fetch
        self.status = status or STATUS_NONE
        self.kwargs = kwargs

    def dump(self):
        # dumper should remove None's from self.kwargs
        return ChoreType(self.name, 'undef', self.kwargs)

    def fetch(self):
        yield
        return

    @classmethod
    def detect(cls, name, url):
        return None

    def __repr__(self):
        return '%r(%s, %s)' % (
            type(self).__name__, self.name,
            ', '.join('%s=%s' % (k, v) for k, v in self.kwargs))

class FeedChore(Chore):
    def __init__(self, name, url, category=None, status=None):
        self.name = name
        self.url = url
        self.category = category
        self.status = status or STATUS_NONE

    def dump(self):
        return ChoreType(self.name, 'feed', {
            'url': self.url,
            'category': self.category
        })

    def fetch(self):
        feed = feedparser.parse(self.url, etag=self.status.last_result)
        last_updated = self.status.updated
        self.status = ChoreStatus(int(time.time()), feed.get('etag'))
        for e in feed.entries:
            evt_time = int(calendar.timegm(e.updated_parsed))
            if last_updated and evt_time > last_updated:
                yield Event(self.name, self.category,
                            evt_time, e.title, e.summary, e.link)

    @classmethod
    def detect(cls, name, url):
        if 'atom' in url or 'rss' in url or 'xml' in url:
            return cls(name, url)
        feed = feedparser.parse(url)
        if feed.entries:
            return cls(name, url)

class GitHubChore(Chore):
    def __init__(self, name, repo, category='release', branch=None, status=None):
        self.name = name
        self.repo = repo
        self.branch = branch
        self.category = category
        self.status = status or STATUS_NONE

    def dump(self):
        return ChoreType(self.name, 'github', {
            'repo': self.repo,
            'branch': self.branch,
            'category': self.category
        })

    def fetch(self):
        if self.category == 'release':
            url = 'https://github.com/%s/releases.atom' % self.repo
        elif self.category == 'tag':
            url = 'https://github.com/%s/tags.atom' % self.repo
        elif self.category == 'commit':
            url = 'https://github.com/%s/commits/%s.atom' % \
                    (self.repo, self.branch or 'master')
        else:
            raise ValueError('unknown category: %s' % self.category)
        feed = feedparser.parse(url, etag=self.status.last_result)
        last_updated = self.status.updated
        self.status = ChoreStatus(int(time.time()), feed.get('etag'))
        for e in feed.entries:
            evt_time = calendar.timegm(e.updated_parsed)
            if last_updated and evt_time > last_updated:
                yield Event(self.name, self.category,
                            evt_time, e.title, e.summary, e.link)

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        assert urlp.netloc == 'github.com'
        pathseg = urlp.path.lstrip('/').split('/')
        if pathseg[0] == 'downloads':
            pathseg.pop(0)
        repo = '/'.join(pathseg[:2])
        if repo.endswith('.git'):
            repo = repo[:-4]
        if len(pathseg) > 2:
            if pathseg[2] == 'releases':
                return cls(name, repo, 'release')
            elif pathseg[2] == 'tags':
                return cls(name, repo, 'tag')
            elif pathseg[2] == 'commits':
                return cls(name, repo, 'commit', pathseg[3])
        for category, url in (
            ('release', 'https://github.com/%s/releases.atom' % repo),
            ('tag', 'https://github.com/%s/tags.atom' % repo),
            ('commit', 'https://github.com/%s/commits/master.atom' % repo)):
            feed = feedparser.parse(url)
            if feed.entries:
                return cls(name, repo, category)

class HTMLSelectorChore(Chore):
    def __init__(self, name, url, selector, regex=None, category=None, status=None):
        self.name = name
        self.url = url
        self.selector = selector
        self.regex = regex and re.compile(regex)
        self.category = category
        self.status = ExtendedChoreStatus(*(status or STATUS_NONE))

    def dump(self):
        return ChoreType(self.name, 'html', {
            'url': self.url,
            'selector': self.selector,
            'regex': self.regex and self.regex.pattern,
            'category': self.category
        })

    def fetch(self):
        lastupd = self.status.load()
        old_entries = lastupd.get('entries')
        old_etag = lastupd.get('etag')
        if old_etag:
            req = HSESSION.get(self.url, headers={'If-None-Match': old_etag},
                    timeout=30)
            if req.status_code == 304:
                self.status = self.status.save(lastupd)
                req.close()
                return
        else:
            req = HSESSION.get(self.url, timeout=30)
        req.raise_for_status()
        lastupd['etag'] = req.headers.get('etag')
        # html5lib for badly escaped sites
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        tags = soup.select(self.selector)
        if not tags:
            warnings.warn("The selector '%s' in '%s' selected nothing." %
                          (self.selector, self.name))
            return
        elif self.regex:
            entries = []
            for x in tags:
                match = self.regex.search(' '.join(x.stripped_strings))
                if match:
                    entries.append(match.group(bool(self.regex.groups)))
        else:
            entries = [x.get_text() for x in tags]
        if not entries:
            warnings.warn("'%s' got nothing." % self.name)
            return
        lastupd['entries'] = entries
        self.status = self.status.save(lastupd)
        if not old_entries or entries == old_entries:
            return
        else:
            diff = tuple(difflib.unified_diff(old_entries, entries, lineterm=''))
            title = '%s website changed' % self.name
            for text in diff:
                if text[0] == '+':
                    title = '%s: %s' % (self.name, text[1:].replace('\n', ' '))
            content = '\n'.join(diff[2:])
        yield Event(self.name, self.category,
                    int(time.time()), title, content, self.url)

class FTPChore(Chore):
    def __init__(self, name, url, category='file', status=None):
        self.name = name
        self.url = url
        self.category = category
        self.status = ExtendedChoreStatus(*(status or STATUS_NONE))

    def dump(self):
        return ChoreType(self.name, 'ftp', {
            'url': self.url,
            'category': self.category
        })

    def fetch(self):
        urlp = urllib.parse.urlparse(self.url)
        lastupd = self.status.load()
        old_entries = lastupd.get('entries')
        with ftputil.FTPHost(urlp.hostname, urlp.username or 'anonymous', urlp.password) as host:
            stat = host.lstat(urlp.path.rstrip('/'))
            if stat.st_mtime != lastupd.get('mtime'):
                lastupd['mtime'] = stat.st_mtime
                entries = host.listdir(urlp.path)
                lastupd['entries'] = entries
        self.status = self.status.save(lastupd)
        if not old_entries or entries == old_entries:
            return
        else:
            diff = tuple(difflib.unified_diff(old_entries, entries, lineterm=''))
            title = '%s FTP directory changed' % self.name
            for text in diff:
                if text[0] == '+':
                    title = '%s: %s' % (self.name, text[1:])
            content = '\n'.join(diff[2:])
        yield Event(self.name, self.category,
                    int(time.time()), title, content, self.url)

class IMAPChore(Chore):
    def __init__(self, name, host, username, password, folder, subject_regex='.*', from_regex='.*', body_regex='.*', category=None, status=None):
        self.name = name
        self.host = host
        self.username = username
        self.password = password
        self.folder = folder
        self.subject_regex = re.compile(subject_regex)
        self.from_regex = re.compile(from_regex)
        self.body_regex = re.compile(body_regex)
        self.category = category
        self.status = status or STATUS_NONE

    def dump(self):
        return ChoreType(self.name, 'imap', {
            'host': self.host,
            'username': self.username,
            'password': self.password,
            'folder': self.folder,
            'subject_regex': self.subject_regex.pattern,
            'from_regex': self.from_regex.pattern,
            'body_regex': self.body_regex.pattern,
            'category': self.category
        })

    def fetch(self):
        raise NotImplementedError

CHORE_HANDLERS = {
    'feed': FeedChore,
    'github': GitHubChore,
    'html': HTMLSelectorChore,
    'imap': IMAPChore,
    'ftp': FTPChore
}

CHORE_PRIO = {
    'feed': 10,
    'github': 9,
    'html': 4,
    'imap': 8,
    'ftp': 5
}

def remove_package_version(name, url, version):
    newurlpspl = ['']
    for s in url.strip('/').split('/'):
        vercheck = urllib.parse.unquote(s).replace(name, '').strip(' -_.')
        if len(vercheck) > 1 and (
            version in vercheck or version.startswith(vercheck)):
            break
        else:
            newurlpspl.append(s)
    return '/'.join(newurlpspl)

RE_SF = re.compile('^(/projects/[^/]+)/(.+)$')

def detect_upstream(name, url, version=None):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        return CHORE_HANDLERS['github'].detect(name, url)
    elif urlp.netloc in ('pypi.io', 'pypi.python.org'):
        try:
            pkgname = os.path.splitext(os.path.basename(urlp.path))[0].rsplit('-', 1)[0]
        except Exception:
            return
        newurl = 'https://pypi.python.org/simple/%s/' % pkgname
        logging.debug('New url: ' + newurl)
        req = HSESSION.get(newurl, timeout=30)
        if req.status_code == 200:
            return HTMLSelectorChore(name, newurl, 'a', None, 'file')
        else:
            return
    elif urlp.scheme == 'ftp':
        newurlp = list(urlp)
        if urlp.path[-1] != '/':
            newurlp[2] = os.path.dirname(urlp.path)
        if version:
            newurlp[2] = remove_package_version(name, newurlp[2], version)
        return FTPChore(name, urllib.parse.urlunparse(newurlp))
    elif urlp.path.rstrip('/').endswith('.git'):
        return
    elif urlp.scheme in ('http', 'https'):
        newurlp = list(urlp)
        if not urlp.query:
            ext = os.path.splitext(urlp.path)[1]
            if ext in frozenset(('.gz', '.bz2', '.xz', '.tar', '.7z', '.rar', '.zip')):
                newurlp[2] = os.path.dirname(urlp.path)
            if urlp.hostname == 'sourceforge.net' and '/files' not in newurlp[2]:
                newurlp[2] = RE_SF.sub(r'\1/files/\2', newurlp[2])
            if version:
                newurlp[2] = remove_package_version(name, newurlp[2], version)
        elif name in urlp.hostname:
            newurlp[2] = '/'
            newurlp[3] = ''
        newurl = urllib.parse.urlunparse(newurlp)
        logging.debug('New url: ' + newurl)
        req = HSESSION.get(newurl, timeout=30)
        if req.status_code != 200:
            newurlp[2] = os.path.dirname(newurlp[2])
            if name in newurlp[2] or name in urlp.hostname:
                newurl = urllib.parse.urlunparse(newurlp)
                logging.debug('New url: ' + newurl)
                req = HSESSION.get(newurl, timeout=30)
                if req.status_code != 200:
                    return
            else:
                return
        newurl = req.url
        if len(req.content) > 1024*1024:
            logging.warning('Webpage too large: ' + newurl)
            return
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        title = None
        if soup.title:
            title = soup.title.string.lower()
            if title.startswith('index of'):
                return HTMLSelectorChore(name, newurl, 'a[href]', None, 'file')
        feedlink = soup.find('a', href=RE_FEED)
        if feedlink:
            return FeedChore(
                name, urllib.parse.urljoin(os.path.dirname(newurl), feedlink['href']))
        if title and 'download' in title:
            return HTMLSelectorChore(name, newurl, 'a[href]', None, 'file')
        githublink = soup.find('a', href=RE_GITHUB)
        if githublink:
            return CHORE_HANDLERS['github'].detect(name, githublink['href'])
    return None

def longest_common_substring(s1, s2):
    m = [[0] * (1 + len(s2)) for i in range(1 + len(s1))]
    longest, x_longest = 0, 0
    for x in range(1, 1 + len(s1)):
        for y in range(1, 1 + len(s2)):
            if s1[x - 1] == s2[y - 1]:
                m[x][y] = m[x - 1][y - 1] + 1
                if m[x][y] > longest:
                    longest = m[x][y]
                    x_longest = x
            else:
                m[x][y] = 0
    return s1[x_longest - longest: x_longest]

URL_FILTERED = frozenset((
    'https', 'com', 'releases', 'org', 'http', 'www', 'net', 'download', 'html',
    'sourceforge', 'pypi', 'projects', 'files', 'software', 'pub', 'git',
    'downloads', 'ftp', 'kernel', 'freedesktop', 'python', 'mozilla', 'cgit',
    'master', 'commits', 'en-us', 'en', 'linux', 'gnu', 'launchpad', 'folder',
    'sort', 'wiki', 'source', 'debian', 'maxresults', 'place', 'tags', 'pipermail',
    'sources', 'php', 'navbar', 'io', 'fedorahosted', 'lists', 'archives',
    'news', 'cgi', 'blog', ''
))

RE_IGN = re.compile(r'v?\d+\.\d+|\d+$')

def detect_name(url, title):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        return urlp.path.strip('/').split('/')[1].lower()
    else:
        urlpath = os.path.splitext(urlp.path.strip('/'))[0].lower().split('/')
        urlkwd = [x for x in urlpath if x not in URL_FILTERED and not RE_IGN.match(x)]
        titlel = title.lower()
        candidates = []
        for k in urlkwd:
            if k in titlel:
                candidates.append(k)
        if candidates:
            return candidates[-1]
        else:
            host = urlp.hostname.split('.')
            cand2 = [x for x in urlp.hostname.split('.') if x not in URL_FILTERED]
            if cand2:
                return cand2[0]
            else:
                return host[-2]

def generate_chore_config(abbs_db=None, bookmarks_html=None, existing=None):
    srcs = collections.OrderedDict()
    src_ver = {}
    failed = []
    if abbs_db:
        logging.info('Reading database...')
        db_abbs = sqlite3.connect(abbs_db)
        cur_abbs = db_abbs.cursor()
        for pkg, url, ver in uniq(cur_abbs.execute(
            "SELECT DISTINCT package_spec.package as package, "
            "package_spec.value as url, packages.version FROM package_spec "
            "LEFT JOIN packages ON package_spec.package=packages.name "
            "WHERE key like '%SRC%' AND (value like 'http%' OR value like 'ftp%') "
            "ORDER BY package ASC"
        ), key=lambda x: x[1]):
            srcs[pkg] = url
            src_ver[pkg] = ver
        db_abbs.close()
    if bookmarks_html:
        logging.info('Reading bookmarks...')
        soup = bs4.BeautifulSoup(open(bookmarks_html, 'rb').read(), 'lxml')
        links = soup.find_all('a')
        for a in links:
            url = a.get('href')
            title = a.string.strip()
            urlp = urllib.parse.urlparse(url)
            if urlp.scheme in ('http', 'https', 'ftp', 'ftps'):
                name = detect_name(url, title)
                srcs[name] = url
            else:
                continue
    chores = existing or collections.OrderedDict()
    logging.info('Detecting upstreams...')
    try:
        for k, v in srcs.items():
            if k in chores:
                continue
            logging.debug('Checking: %s, %s' % (k, v))
            try:
                chore = detect_upstream(k, v, src_ver.get(k))
                if chore:
                    chore_dump = chore.dump()
                    chores[k] = {k:v for k,v in chore_dump.kwargs.items() if v}
                    chores[k]['chore'] = chore_dump.chore
                else:
                    failed.append('%s, %s' % (k, v))
                    logging.warning('Failed to find upstream: %s, %s' % (k, v))
            except Exception:
                logging.exception('Error when checking %s, %s' % (k, v))
    except KeyboardInterrupt:
        logging.warning('Interrupted.')
    return chores, failed
