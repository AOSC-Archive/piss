#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import calendar
import collections
import urllib.parse

import feedparser

News = collections.namedtuple('News', 'upstream category time title content url')

class Upstream:
    def __init__(self, name, url, category='all'):
        self.name = name
        self.url = url
        self.subs_type = None
        self.category = category

    def get_updates(self):
        return []

    def __repr__(self):
        return '%r(%s, %s, %s)' % (
                type(self).__name__, self.name, self.url, self.category)

class GitHubUpsteam(Upstream):
    def __init__(self, name, url, category='release'):
        self.name = name
        self.subs_type = 'github'
        self.category = category
        urlp = urllib.parse.urlparse(url)
        assert urlp.netloc == 'github.com'
        self.repo = '/'.join(urlp.path.lstrip('/').split('/')[:2])
        self.url = 'https://github.com/' + self.repo

    def get_updates(self):
        if self.category == 'release':
            url = 'https://github.com/%s/releases.atom' % self.repo
        elif self.category == 'tag':
            url = 'https://github.com/%s/tags.atom' % self.repo
        elif self.category == 'commit':
            url = ('https://github.com/%s/commits/master.atom' % self.repo)
        else:
            raise ValueError('unknown category: %s' % category)
        feed = feedparser.parse(url)
        updates = [News(self.name, self.category,
                        calendar.timegm(e.updated_parsed), e.title,
                        e.summary, e.link) for e in feed.entries]
        return updates

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        assert urlp.netloc == 'github.com'
        repo = '/'.join(os.path.split(urlp.path.lstrip('/'))[:2])
        for category, url in (
            ('release', 'https://github.com/%s/releases.atom' % repo),
            ('tag', 'https://github.com/%s/tags.atom' % repo)):
            feed = feedparser.parse(url)
            if feed.entries:
                return cls(name, url, category)
        return cls(name, url, 'commit')

class FTPUpsteam(Upstream):
    def get_updates(self):
        ...

UPSTREAM_HANDLERS = {
    'github': GitHubUpsteam,
    'ftp': FTPUpsteam
}

def detect_upstream(name, url, version=None):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        return UPSTREAM_HANDLERS['github'].detect(name, url)
    #elif urlp.scheme == 'ftp':
        #return UPSTREAM_HANDLERS['ftp'].detect(name, url)
    return None
