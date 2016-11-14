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
import markupsafe

__version__ = '0.2'

USER_AGENT = 'Mozilla/5.0 (compatible; PISS/%s; +https://github.com/AOSC-Dev/piss)' % __version__

RE_FEED = re.compile("(^|\W)(atom|rss|feed(?!back))", re.I)
RE_GITHUB = re.compile("github.com", re.I)

COMMON_EXT = frozenset(('.gz', '.bz2', '.xz', '.tar', '.7z', '.rar', '.zip'))

DATETIME_FMTs = (
(re.compile(r'\d+-[A-S][a-y]{2}-\d{4} \d+:\d{2}'), "%d-%b-%Y %H:%M"),
(re.compile(r'\d{4}-\d+-\d+ \d+:\d{2}'), "%Y-%m-%d %H:%M"),
(re.compile(r'\d{4}-[A-S][a-y]{2}-\d+ \d+:\d{2}:\d{2}'), "%Y-%b-%d %H:%M:%S"),
(re.compile(r'[F-W][a-u]{2} [A-S][a-y]{2} +\d+ \d{2}:\d{2}:\d{2} \d{4}'), "%a %b %d %H:%M:%S %Y"),
(re.compile(r'\d{4}-\d+-\d+'), "%Y-%m-%d")
)

RE_FILESIZE = re.compile(r'\d+(\.\d+)? ?[BKMGTPEZY]|\d+|-', re.I)
RE_ABSPATH = re.compile(r'^((ht|f)tps?:/)?/')
RE_COMMONHEAD = re.compile('Name|(Last )?modified|Size|Description|Metadata|Type|Parent Directory', re.I)
RE_HASTEXT = re.compile('.+')

RE_HTMLTAG = re.compile('</?[^>]+>')

FileEntry = collections.namedtuple('FileEntry', 'name modified size description')

ChoreType = collections.namedtuple('ChoreType', ('name', 'chore', 'kwargs'))
ChoreStatus = collections.namedtuple('ChoreStatus', ('updated', 'last_result'))

STATUS_NONE = ChoreStatus(0, None)

Event = collections.namedtuple('Event', (
    'chore',    # Chore name
    'category', # Category: commit, issue, pr, tag, release, news
    'time',     # Original unix timestamp of the event message.
    'title',    # Title of the event message.
    'content',  # Content of the event message, HTML.
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

def sizeof_fmt(num, suffix='B'):
    for unit in ['','Ki','Mi','Gi','Ti','Pi','Ei','Zi']:
        if abs(num) < 1024:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)

def human2bytes(s):
    """
    >>> human2bytes('1M')
    1048576
    >>> human2bytes('1G')
    1073741824
    """
    if s is None:
        return None
    try:
        return int(s)
    except ValueError:
        symbols = 'BKMGTPEZY'
        letter = s[-1:].strip().upper()
        num = float(s[:-1])
        prefix = {symbols[0]: 1}
        for i, s in enumerate(symbols[1:]):
            prefix[s] = 1 << (i+1)*10
        return int(num * prefix[letter])

def parse_listing(soup):
    '''
    Try to parse apache/nginx-style directory listing with all kinds of tricks.

    Exceptions or an empty listing suggust a failure.
    We strongly recommend generating the `soup` with 'html5lib'.
    '''
    cwd = None
    listing = []
    if soup.title and soup.title.string.startswith('Index of '):
        cwd = soup.title.string[9:]
    elif soup.h1:
        title = soup.h1.get_text().strip()
        if title.startswith('Index of '):
            cwd = title.string[9:]
    [img.decompose() for img in soup.find_all('img')]
    file_name = file_mod = file_size = file_desc = None
    pres = [x for x in soup.find_all('pre') if
            x.find('a', string=RE_HASTEXT)]
    tables = [x for x in soup.find_all('table') if
              x.find(string=RE_COMMONHEAD)] if not pres else ()
    heads = []
    if pres:
        pre = pres[0]
        started = False
        for element in (pre.hr.next_siblings if pre.hr else pre.children):
            if element.name == 'a':
                if not element.string or not element.string.strip():
                    continue
                elif started:
                    if file_name:
                        listing.append(FileEntry(
                            file_name, file_mod, file_size, file_desc))
                    file_name = urllib.parse.unquote(element['href'])
                    file_mod = file_size = file_desc = None
                elif (element.string in ('Parent Directory', '..', '../') or
                      element['href'][0] not in '?/'):
                    started = True
            elif not element.name:
                line = element.string.replace('\r', '').split('\n', 1)[0].lstrip()
                for regex, fmt in DATETIME_FMTs:
                    match = regex.match(line)
                    if match:
                        file_mod = time.strptime(match.group(0), fmt)
                        line = line[match.end():].lstrip()
                        break
                match = RE_FILESIZE.match(line)
                if match:
                    sizestr = match.group(0)
                    if sizestr == '-':
                        file_size = None
                    else:
                        file_size = human2bytes(sizestr.replace(' ', ''))
                    line = line[match.end():].lstrip()
                if line:
                    file_desc = line.rstrip()
                    if file_name and file_desc == '/':
                        file_name += '/'
                        file_desc = None
            else:
                continue
        if file_name:
            listing.append(FileEntry(file_name, file_mod, file_size, file_desc))
    elif tables:
        started = False
        for tr in tables[0].find_all('tr'):
            status = 0
            file_name = file_mod = file_size = file_desc = None
            if started:
                if tr.parent.name in ('thead', 'tfoot') or tr.th:
                    continue
                for td in tr.find_all('td'):
                    if td.get('colspan'):
                        continue
                    elif status == 0:
                        if not td.a:
                            continue
                        a_str = td.a.get_text().strip()
                        a_href = td.a['href']
                        if not a_str or not a_href:
                            continue
                        elif a_str == 'Parent Directory' or a_href == '../':
                            break
                        else:
                            file_name = urllib.parse.unquote(a_href)
                            status = 1
                    elif heads[status] == 'modified':
                        timestr = td.get_text().strip()
                        if timestr:
                            for regex, fmt in DATETIME_FMTs:
                                if regex.match(timestr):
                                    file_mod = time.strptime(timestr, fmt)
                                    break
                            else:
                                if td.get('data-sort-value'):
                                    file_mod = time.gmtime(int(td['data-sort-value']))
                                else:
                                    raise AssertionError(
                                        "can't identify date/time format")
                        status += 1
                    elif heads[status] == 'size':
                        sizestr = td.get_text().strip()
                        if sizestr == '-' or not sizestr:
                            file_size = None
                        elif td.get('data-sort-value'):
                            file_size = int(td['data-sort-value'])
                        else:
                            match = RE_FILESIZE.match(sizestr)
                            if match:
                                file_size = human2bytes(
                                    match.group(0).replace(' ', ''))
                            else:
                                file_size = None
                        status += 1
                    elif heads[status] == 'description':
                        file_desc = file_desc or ''.join(map(str, td.children)
                                        ).strip('\xa0').strip() or None
                        status += 1
                    elif status:
                        # unknown header
                        status += 1
                if file_name:
                    listing.append(FileEntry(
                        file_name, file_mod, file_size, file_desc))
            elif tr.hr:
                started = True
                continue
            elif tr.find(string=RE_COMMONHEAD):
                for th in (tr.find_all('th') if tr.th else tr.find_all('td')):
                    name = th.get_text().strip('\xa0').strip().lower()
                    if not name:
                        continue
                    elif name in ('name', 'size', 'description'):
                        heads.append(name)
                    elif name.endswith('name') or name.startswith('file'):
                        heads.append('name')
                    elif name.endswith('modified') or name.startswith('uploaded'):
                        heads.append('modified')
                    elif 'size' in name:
                        heads.append('size')
                    elif name.endswith('signature'):
                        heads.append('signature')
                    else:
                        heads.append('description')
                if not heads or 'name' not in heads:
                    heads = ('name', 'modified', 'size', 'description')
                started = True
                continue
    elif soup.ul:
        for li in soup.ul.find_all('li'):
            a = li.a
            if not a or not a.get('href'):
                continue
            file_name = urllib.parse.unquote(a['href'])
            if (file_name in ('Parent Directory', '.', './', '..', '../', '#')
                or RE_ABSPATH.match(file_name)):
                continue
            else:
                listing.append(FileEntry(file_name, None, None, None))
    return cwd, listing

class ExtendedChoreStatus(ChoreStatus):
    def load(self):
        return json.loads(self.last_result or '{}')

    def save(self, update_time, d):
        return ExtendedChoreStatus(update_time, json.dumps(d))

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
            ', '.join('%s=%s' % (k, v) for k, v in self.dump().kwargs))

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
        fetch_time = int(time.time())
        feed = feedparser.parse(self.url, etag=self.status.last_result)
        last_updated = self.status.updated
        self.status = ChoreStatus(fetch_time, feed.get('etag'))
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
        fetch_time = int(time.time())
        feed = feedparser.parse(url, etag=self.status.last_result)
        last_updated = self.status.updated
        self.status = ChoreStatus(fetch_time, feed.get('etag'))
        for e in feed.entries:
            evt_time = calendar.timegm(e.updated_parsed)
            if last_updated and evt_time > last_updated:
                yield Event(self.name, self.category,
                            evt_time, e.title, e.summary, e.link)

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        if urlp.netloc != 'github.com':
            return
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

class BitbucketChore(Chore):
    def __init__(self, name, repo, category='release', status=None):
        self.name = name
        self.repo = repo
        # 'release' -> 'downloads'
        # 'tag' -> 'tags'
        self.category = category
        self.status = status or STATUS_NONE

    def dump(self):
        return ChoreType(self.name, 'bitbucket', {
            'repo': self.repo,
            'category': self.category
        })

    def fetch(self):
        if self.category == 'release':
            url = 'https://api.bitbucket.org/2.0/repositories/%s/downloads' % self.repo
        elif self.category == 'tag':
            url = 'https://api.bitbucket.org/2.0/repositories/%s/refs/tags' % self.repo
        else:
            raise ValueError('unknown category: %s' % self.category)
        last_updated = self.status.updated
        old_etag = self.status.last_result
        fetch_time = int(time.time())
        if old_etag:
            req = HSESSION.get(url, headers={'If-None-Match': old_etag},
                    timeout=30)
            if req.status_code == 304:
                self.status = ChoreStatus(fetch_time, old_etag)
                req.close()
                return
        else:
            req = HSESSION.get(url, timeout=30)
        req.raise_for_status()
        self.status = ChoreStatus(fetch_time, req.headers.get('etag'))
        d = req.json()
        if self.category == 'release':
            title = None
            messages = []
            latest = 0
            for item in d['values']:
                # throw ugly parsing work to feedparser
                evt_time_tup = feedparser._parse_date(item['created_on'])
                evt_time = calendar.timegm(evt_time_tup)
                latest = max(evt_time, latest)
                if last_updated and evt_time > last_updated:
                    if not title:
                        title = item['name']
                    messages.append(markupsafe.Markup(
                        '<li><a href="%s">%s</a>, %s, %s by <a href="%s">%s</a></li>'
                        % (
                        item['links']['html']['href'],
                        item['name'], sizeof_fmt(d['values'][0]['size']),
                        time.strftime('%Y-%m-%d', evt_time_tup),
                        item['links']['html'], item['user']['username'])
                    ))
            if messages:
                yield Event(self.name, self.category, latest, title,
                            markupsafe.Markup('<ul>%s</ul>') %
                            markupsafe.Markup('').join(messages),
                            'https://bitbucket.org/%s/downloads' % self.repo)
        else:
            for item in d['values']:
                evt_time = calendar.timegm(
                    feedparser._parse_date(item['target']['date']))
                if last_updated and evt_time > last_updated:
                    yield Event(self.name, self.category, evt_time, item['name'],
                                markupsafe.Markup('<pre>%s</pre>') %
                                item['target']['message'],
                                item['links']['html']['href'])

    @classmethod
    def detect(cls, name, url):
        urlp = urllib.parse.urlparse(url)
        if urlp.netloc != 'bitbucket.org':
            return
        pathseg = urlp.path.lstrip('/').split('/')
        repo = '/'.join(pathseg[:2])
        if repo.endswith('.git'):
            repo = repo[:-4]
        if len(pathseg) > 2:
            if pathseg[2] == 'downloads':
                return cls(name, repo, 'release')
            elif pathseg[2] == 'get':
                return cls(name, repo, 'tag')
        for category, url in (
            ('release', 'https://api.bitbucket.org/2.0/repositories/%s/downloads' % repo),
            ('tag', 'https://api.bitbucket.org/2.0/repositories/%s/refs/tags' % repo)):
            req = HSESSION.get(url, timeout=30)
            if req.status_code == 200:
                d = req.json()
                if d.get('values'):
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
        fetch_time = int(time.time())
        if old_etag:
            req = HSESSION.get(self.url, headers={'If-None-Match': old_etag},
                    timeout=30)
            if req.status_code == 304:
                self.status = self.status.save(fetch_time, lastupd)
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
                match = self.regex.search(' '.join(x.stripped_strings).strip())
                if match:
                    entries.append(match.group(bool(self.regex.groups)))
        else:
            entries = [' '.join(x.stripped_strings).strip() for x in tags]
        if not entries:
            warnings.warn("'%s' got nothing." % self.name)
            return
        lastupd['entries'] = entries
        self.status = self.status.save(fetch_time, lastupd)
        if not old_entries or entries == old_entries:
            return
        else:
            diff = tuple(difflib.unified_diff(old_entries, entries, lineterm=''))
            title = '%s website changed' % self.name
            for text in diff:
                if text[0] == '+':
                    title = text[1:].replace('\r', '').replace('\n', ' ')
                    break
            content = (markupsafe.Markup('<pre>%s</pre>') % '\n'.join(diff[2:]))
        yield Event(self.name, self.category,
                    fetch_time, title, content, self.url)

class DirListingChore(Chore):
    '''Handle Apache/nginx-style directory listing pages.'''

    def __init__(self, name, url, category='file', status=None):
        self.name = name
        self.url = url
        self.category = category
        self.status = ExtendedChoreStatus(*(status or STATUS_NONE))

    def dump(self):
        return ChoreType(self.name, 'dirlist', {
            'url': self.url,
            'category': self.category
        })

    def fetch(self):
        lastupd = self.status.load()
        last_updated = self.status.updated
        old_etag = lastupd.get('etag')
        fetch_time = int(time.time())
        if old_etag:
            req = HSESSION.get(self.url, headers={'If-None-Match': old_etag},
                    timeout=30)
            if req.status_code == 304:
                self.status = self.status.save(fetch_time, lastupd)
                req.close()
                return
        else:
            req = HSESSION.get(self.url, timeout=30)
        req.raise_for_status()
        lastupd['etag'] = req.headers.get('etag')
        # html5lib for badly escaped sites
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        cwd, listing = parse_listing(soup)
        title = None
        if any(x.modified for x in listing):
            self.status = self.status.save(fetch_time, lastupd)
            messages = []
            latest = 0
            for item in listing:
                evt_time_tup = item.modified or time.gmtime(0)
                evt_time = calendar.timegm(evt_time_tup)
                latest = max(evt_time, latest)
                if last_updated and evt_time > last_updated:
                    if not title:
                        title = item.name.rstrip('/')
                    attrs = [time.strftime('%Y-%m-%d', evt_time_tup)]
                    if item.size is not None:
                        attrs.append(sizeof_fmt(item.size))
                    if item.description is not None:
                        attrs.append(RE_HTMLTAG.sub('', item.description))
                    messages.append(markupsafe.Markup(
                        '<li><a href="%s">%s</a>, %s</li>' % (
                        urllib.parse.urljoin(self.url, urllib.parse.quote(item.name)),
                        item.name, ', '.join(attrs))
                    ))
            if messages:
                yield Event(self.name, self.category, latest, title,
                            markupsafe.Markup('<ul>%s</ul>') %
                            markupsafe.Markup('').join(messages),
                            self.url)
        else:
            entries = [RE_HTMLTAG.sub('', '\t'.join(map(str,
                       filter(lambda x: x is not None, i)))) for i in listing]
            if not entries:
                warnings.warn("'%s' got nothing." % self.name)
                return
            old_entries = lastupd.get('entries')
            lastupd['entries'] = entries
            self.status = self.status.save(fetch_time, lastupd)
            if not old_entries or entries == old_entries:
                return
            else:
                diff = tuple(difflib.unified_diff(old_entries, entries, lineterm=''))
                title = '%s files changed' % self.name
                for text in diff:
                    if text[0] == '+':
                        title = text[1:].replace('\n', ' ').rstrip('/')
                        break
                content = (markupsafe.Markup('<pre>%s</pre>') % '\n'.join(diff[2:]))
            yield Event(self.name, self.category,
                        fetch_time, title, content, self.url)

    @classmethod
    def detect(cls, name, url):
        req = HSESSION.get(url, timeout=30)
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        try:
            cwd, listing = parse_listing(soup)
        except Exception:
            return
        if listing:
            return cls(name, url)
        else:
            return

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
        fetch_time = int(time.time())
        with ftputil.FTPHost(urlp.hostname, urlp.username or 'anonymous', urlp.password) as host:
            stat = host.lstat(urlp.path.rstrip('/'))
            if stat.st_mtime == lastupd.get('mtime'):
                return
            else:
                lastupd['mtime'] = stat.st_mtime
                entries = sorted(host.listdir(urlp.path))
                lastupd['entries'] = entries
        self.status = self.status.save(fetch_time, lastupd)
        if not old_entries or entries == old_entries:
            return
        else:
            diff = tuple(difflib.unified_diff(old_entries, entries, lineterm=''))
            title = '%s FTP directory changed' % self.name
            for text in diff:
                if text[0] == '+':
                    title = text[1:]
                    break
            content = (markupsafe.Markup('<pre>%s</pre>') % '\n'.join(diff[2:]))
        yield Event(self.name, self.category,
                    lastupd.get('mtime') or fetch_time, title, content, self.url)

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
    'bitbucket': BitbucketChore,
    'html': HTMLSelectorChore,
    'dirlist': DirListingChore,
    'imap': IMAPChore,
    'ftp': FTPChore
}

CHORE_PRIO = {
    'feed': 10,
    'github': 9,
    'bitbucket': 9,
    'dirlist': 6,
    'html': 4,
    'imap': 8,
    'ftp': 5
}

def remove_package_version(name, url, version):
    newurlpspl = ['']
    for s in url.strip('/').split('/'):
        vercheck = urllib.parse.unquote(s).replace(name, '').strip(' -_.')
        if len(vercheck) > 1 and (
            version in vercheck or
            (not RE_VER_MINOR.match(vercheck) and version.startswith(vercheck))):
            break
        elif s:
            newurlpspl.append(s)
    return '/'.join(newurlpspl) + '/'

RE_SF = re.compile('^(/projects/[^/]+)/(.+)$')

def detect_upstream(name, url, version=None):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        return CHORE_HANDLERS['github'].detect(name, url)
    elif urlp.netloc == 'bitbucket.org':
        return CHORE_HANDLERS['bitbucket'].detect(name, url)
    elif urlp.netloc in ('pypi.io', 'pypi.python.org'):
        try:
            pkgname = os.path.splitext(os.path.basename(urlp.path))[0].rsplit('-', 1)[0]
        except Exception:
            return
        if not pkgname:
            logging.debug('Package name not found: ' + url)
            return
        newurl = 'https://pypi.python.org/simple/%s/' % pkgname
        logging.debug('New url: ' + newurl)
        req = HSESSION.get(newurl, timeout=30)
        if req.status_code == 200:
            return HTMLSelectorChore(name, newurl, 'a', None, 'file')
        else:
            return
    elif urlp.netloc == 'launchpad.net':
        projname = urlp.path.strip('/').split('/')[0].lower()
        if name in projname or projname in name:
            return FeedChore(name,
                'http://feeds.launchpad.net/%s/announcements.atom' % projname, 'news')
    elif urlp.scheme == 'ftp':
        newurlp = list(urlp)
        if urlp.path[-1] != '/':
            newurlp[2] = os.path.dirname(urlp.path) + '/'
        if version:
            newurlp[2] = remove_package_version(name, newurlp[2], version)
        return FTPChore(name, urllib.parse.urlunparse(newurlp))
    elif urlp.path.rstrip('/').endswith('.git'):
        return
    elif urlp.scheme in ('http', 'https'):
        newurlp = list(urlp)
        category = None
        if not urlp.query:
            ext = os.path.splitext(urlp.path)[1]
            if ext in COMMON_EXT:
                newurlp[2] = os.path.dirname(urlp.path)
                if newurlp[2] != '/':
                    newurlp[2] += '/'
            if urlp.hostname == 'sourceforge.net':
                path = newurlp[2].strip('/').split('/')
                if path[0] == 'projects':
                    filepath = '/' + '/'.join(path[3:])
                    return FeedChore(name,
                            'https://sourceforge.net/projects/%s/rss?path=%s' %
                            (path[1], filepath), 'file')
                elif path[0] == 'code-snapshots':
                    return FeedChore(name,
                            'https://sourceforge.net/projects/%s/rss?path=/' %
                            path[4], 'file')
            elif urlp.hostname in ('downloads.sourceforge.net', 
                'prdownloads.sourceforge.net', 'download.sourceforge.net'):
                path = newurlp[2].strip('/').split('/')
                if path[0] == 'project':
                    filepath = '/' + '/'.join(path[2:])
                    return FeedChore(name,
                            'https://sourceforge.net/projects/%s/rss?path=%s' %
                            (path[1], filepath), 'file')
                elif path[0] == 'sourceforge':
                    return FeedChore(name,
                            'https://sourceforge.net/projects/%s/rss?path=/' %
                            path[1], 'file')
                else:
                    return FeedChore(name,
                            'https://sourceforge.net/projects/%s/rss?path=/' %
                            path[0], 'file')
            if version:
                newurlp[2] = remove_package_version(name, newurlp[2], version)
        elif name in urlp.hostname:
            newurlp[2] = '/'
            newurlp[3] = ''
        newurlp[5] = ''
        newurl = urllib.parse.urlunparse(newurlp)
        logging.debug('New url: ' + newurl)
        req = HSESSION.get(newurl, timeout=30)
        if req.status_code != 200:
            newurlp[2] = os.path.dirname(newurlp[2].rstrip('/'))
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
            title = soup.title.string
            if title.startswith('Index of'):
                return DirListingChore(name, newurl, 'file')
        feedlink = soup.find('a', href=RE_FEED)
        if feedlink:
            return FeedChore(name,
                    urllib.parse.urljoin(os.path.dirname(newurl), feedlink['href']),
                    category)
        if urlp.hostname.endswith('.sourceforge.net'):
            return FeedChore(name,
                    'https://sourceforge.net/projects/%s/rss?path=/' %
                    urlp.hostname.split('.', 1)[0], 'file')
        if title and 'download' in title.lower():
            ch = CHORE_HANDLERS['dirlist'].detect(name, newurl)
            if ch:
                return ch
            else:
                return HTMLSelectorChore(name, newurl, 'a[href]', None, 'file')
        githublink = soup.find('a', href=RE_GITHUB)
        if githublink:
            return CHORE_HANDLERS['github'].detect(name, githublink['href'])
    return None

URL_FILTERED = frozenset((
    'https', 'com', 'releases', 'org', 'http', 'www', 'net', 'download', 'html',
    'sourceforge', 'pypi', 'projects', 'files', 'software', 'pub', 'git',
    'downloads', 'ftp', 'kernel', 'freedesktop', 'python', 'mozilla', 'cgit',
    'master', 'commits', 'en-us', 'en', 'linux', 'gnu', 'launchpad', 'folder',
    'sort', 'wiki', 'source', 'debian', 'maxresults', 'place', 'tags', 'pipermail',
    'sources', 'php', 'navbar', 'io', 'fedorahosted', 'lists', 'archives',
    'news', 'cgi', 'blog', ''
))

RE_IGN = re.compile(r'v?\d+\.\d+|\d+$|.$')
RE_VER_MINOR = re.compile(r'\d+\.\d+$')

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
            logging.info('Checking: %s, %s' % (k, v))
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
