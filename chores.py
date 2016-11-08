#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
import difflib
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

ChoreType = collections.namedtuple('ChoreType', ('name', 'chore', 'kwargs'))
ChoreStatus = collections.namedtuple('ChoreStatus', ('updated', 'last_result'))

STATUS_NONE = ChoreStatus(None, None)

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

class ExtendedChoreStatus(ChoreStatus):
    def load(self):
        return json.loads(self.last_result or '{}')

    def save(self, d):
        return ExtendedChoreStatus(int(time.time()), json.dumps(d))

class Chore:
    def __init__(self, name, status=None, **kwargs):
        self.name = name
        self.status = status or STATUS_NONE
        self.kwargs = kwargs

    def dump(self):
        # dumper should remove None's from self.kwargs
        return ChoreType(self.name, 'undef', self.kwargs)

    def status(self):
        return self.status

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
        self.status = ChoreStatus(int(time.time()), feed.get('etag'))
        for e in feed.entries:
            yield Event(self.name, self.category,
                        calendar.timegm(e.updated_parsed), e.title,
                        e.summary, e.link)

    @classmethod
    def detect(cls, name, url):
        if 'atom' in url or 'rss' in url or 'xml' in url:
            return cls(name, url)
        feed = feedparser.parse(url)
        if feed.entries:
            return cls(name, url)

class GitHubChore(Chore):
    def __init__(self, name, repo, category='release', status=None):
        self.name = name
        self.repo = repo
        self.category = category
        self.status = status or STATUS_NONE

    def dump(self):
        return ChoreType(self.name, 'github', {
            'repo': self.repo,
            'category': self.category
        })

    def fetch(self):
        if self.category == 'release':
            url = 'https://github.com/%s/releases.atom' % self.repo
        elif self.category == 'tag':
            url = 'https://github.com/%s/tags.atom' % self.repo
        else:
            raise ValueError('unknown category: %s' % self.category)
        feed = feedparser.parse(url, etag=self.status.last_result)
        self.status = ChoreStatus(int(time.time()), feed.get('etag'))
        for e in feed.entries:
            yield Event(self.name, self.category,
                        calendar.timegm(e.updated_parsed), e.title,
                        e.summary, e.link)

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        assert urlp.netloc == 'github.com'
        pathseg = urlp.path.lstrip('/').split('/')
        repo = '/'.join(pathseg[:2])
        if len(pathseg) > 2:
            if pathseg[2] == 'releases':
                return cls(name, repo, 'release')
            elif pathseg[2] == 'tags':
                return cls(name, repo, 'release')
            elif pathseg[2] == 'commits':
                return FeedChore(
                    name,
                    'https://github.com/%s/commits/%s.atom' % (repo, pathseg[3]),
                    'commit'
                )
        for category, url in (
            ('release', 'https://github.com/%s/releases.atom' % repo),
            ('tag', 'https://github.com/%s/tags.atom' % repo)):
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

        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def dump(self):
        return ChoreType(self.name, 'html', {
            'url': self.url,
            'selector': self.selector,
            'regex': self.regex.pattern,
            'category': self.category
        })

    def fetch(self):
        lastupd = self.status.load()
        old_entries = lastupd.get('entries')
        old_etag = lastupd.get('etag')
        if old_etag:
            req = self.session.get(self.url, headers={'If-None-Match': old_etag},
                    timeout=30)
            if req.status_code == 304:
                self.status = self.status.save(lastupd)
                return
        else:
            req = self.session.get(self.url, timeout=30)
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
                match = self.regex.search(x.get_text())
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
                    title = '%s: %s' % (self.name, text[1:])
            content = '\n'.join(diff[2:])
        yield Event(self.name, self.category,
                    time.time(), title, content, self.url)

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
                    time.time(), title, content, self.url)

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        if urlp.scheme == 'ftp':
            newurlp = list(urlp)
            if urlp.path[-1] != '/':
                newurlp[2] = os.path.dirname(urlp.path)
            return cls(name, urllib.parse.urlunparse(newurlp))

UPSTREAM_HANDLERS = {
    'feed': FeedChore,
    'github': GitHubChore,
    'html': HTMLSelectorChore,
    'github': GitHubChore,
    'ftp': FTPChore
}

def detect_upstream(name, url):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        return UPSTREAM_HANDLERS['github'].detect(name, url)
    elif urlp.scheme == 'ftp':
        return UPSTREAM_HANDLERS['ftp'].detect(name, url)
    elif urlp.scheme in ('http', 'https'):
        newurlp = list(urlp)
        if not urlp.query:
            ext = os.path.splitext(urlp.path)[1]
            if ext in frozenset(('.gz', '.bz2', '.xz', '.tar', '.7z', '.rar', '.zip')):
                newurlp[2] = os.path.dirname(urlp.path)
        newurl = urllib.parse.urlunparse(newurlp)
        req = HSESSION.get(newurl, timeout=30)
        if req.status_code != 200:
            return None
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        if soup.title and soup.title.string.lower().startswith('Index of'):
            return HTMLSelectorChore(name, url, 'a[href]', None, 'file')
        feedlink = soup.find('a', href=RE_FEED)
        if feedlink:
            return FeedChore(
                name, os.path.join(os.path.dirname(newurl), feedlink['href']))
    return None
