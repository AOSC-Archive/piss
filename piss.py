#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import io
import re
import gc
import sys
import time
import socket
import sqlite3
import logging
import argparse
import calendar
import functools
import collections
import urllib.parse

import anitya
from htmllistparse import parse as parse_listing

import bs4
import ftputil
import requests
import feedparser

__version__ = '1.0'

logging.basicConfig(
    format='[%(levelname)s] %(message)s', level=logging.INFO)
logging.captureWarnings(True)

USER_AGENT = 'Mozilla/5.0 (compatible; PISS/%s; +https://github.com/AOSC-Dev/piss)' % __version__

RE_ALL_DIGITS_OR_NOT = re.compile("\d+|\D+")
RE_DIGITS = re.compile("\d+")
RE_ALPHA = re.compile("[A-Za-z]")
RE_CHARCLASS = re.compile("([A-Za-z]+|\d+|[._+~-]+)")
RE_SRCHOST = re.compile(r'^https://(github\.com|bitbucket\.org|gitlab\.com)')
RE_PYPI = re.compile(r'^https?://pypi\.(python\.org|io)')
RE_PYPISRC = re.compile(r'^https?://pypi\.(python\.org|io)/packages/source/')
RE_VER_PREFIX = re.compile(r'^(?:version|ver|v|releases|release|rel|r)[._/-]?', re.I)
RE_TARBALL = re.compile(r'^(.+?)[._-][vr]?(\d.*?)(?:[._-](?:orig|src|source))?(\.tar\.xz|\.tar\.bz2|\.tar\.gz|\.t.z|\.zip|\.gem)$', re.I)
RE_TARBALL_GROUP = lambda s: re.compile(r'\b(' + (re.escape(s) if s else '(.+?)') + r'[._-][vr]?(?:\d.*?)(?:[._-](?:orig|src|source))?(?:\.tar\.xz|\.tar\.bz2|\.tar\.gz|\.t.z|\.zip))\b', re.I)
RE_BINARY = re.compile('[._+-](linux32|linux64|windows|win32|win64|win\b|w32|w64|mingw|msvc|mac|osx|darwin|ios|x86|i.86|x64|amd64|arm64|armhf|armel|mips|ppc|powerpc|s390x|portable|dbgsym)', re.I)
RE_VER_MINOR = re.compile(r'\d+\.\d+$')
RE_CGIT_TAGS = re.compile(r'/tag/\?h=|refs/tags/')

RE_ALPHAPREFIX = re.compile("^[A-Za-z_.-]{5,}")
RE_VERSION = re.compile(r"\d+\.\d+|\d{3,}")

COMMON_EXT = frozenset(('.gz', '.bz2', '.xz', '.tar', '.7z', '.rar', '.zip', '.tgz', '.tbz', '.txz'))
CGIT_SITES = frozenset((
'git.kernel.org',
'git.zx2c4.com',
'git.gnome.org',
'git.deluge-torrent.org',
'git.netsurf-browser.org',
'git.archlinux.org',
'git.torproject.org',
'git.opensvc.com',
'repo.or.cz',
))

socket.setdefaulttimeout(30)

strptime_iso = lambda s: int(calendar.timegm(feedparser._parse_date(s)))

HSESSION = requests.Session()
HSESSION.headers['User-Agent'] = USER_AGENT

class Release(collections.namedtuple(
    'Release', 'package upstreamtype version updated url')):
    def __new__(cls, package, upstreamtype, version, updated, url):
        ver = RE_VER_PREFIX.sub('', version)
        ver = re.sub('^' + re.escape(package) + '[._-]', '', ver)
        if '.' not in ver:
            ver = ver.replace('_', '.')
        return super().__new__(cls, package, upstreamtype, ver, updated, url)

Tarball = collections.namedtuple('Tarball', 'filename updated desc')
SCMTag = collections.namedtuple('SCMTag', 'name updated desc')

class EmptyContent(ValueError):
    pass

cmp = lambda a, b: ((a > b) - (a < b))

def version_compare(a, b):
    def _order(x):
        """Return an integer value for character x"""
        if x == '~':
            return -1
        elif RE_DIGITS.match(x):
            return int(x) + 1
        elif RE_ALPHA.match(x):
            return ord(x)
        else:
            return ord(x) + 256

    def _version_cmp_string(va, vb):
        la = [_order(x) for x in va]
        lb = [_order(x) for x in vb]
        while la or lb:
            a = b = 0
            if la:
                a = la.pop(0)
            if lb:
                b = lb.pop(0)
            if a < b:
                return -1
            elif a > b:
                return 1
        return 0

    def _version_cmp_part(va, vb):
        la = RE_ALL_DIGITS_OR_NOT.findall(va)
        lb = RE_ALL_DIGITS_OR_NOT.findall(vb)
        while la or lb:
            a = b = "0"
            if la:
                a = la.pop(0)
            if lb:
                b = lb.pop(0)
            if RE_DIGITS.match(a) and RE_DIGITS.match(b):
                a = int(a)
                b = int(b)
                if a < b:
                    return -1
                elif a > b:
                    return 1
            else:
                res = _version_cmp_string(a, b)
                if res != 0:
                    return res
        return 0

    return _version_cmp_part(a, b) or cmp(a, b)

version_compare_key = functools.cmp_to_key(version_compare)

def version_format(version):
    if not version:
        return re.compile('')
    ret = []
    for s in RE_CHARCLASS.split(version):
        if not s:
            pass
        elif s.isdigit():
            if len(s) < 3:
                ret.append(r'\d+')
            else:
                ret.append(r'\d{3,}')
        elif s.isalpha():
            ret.append('[A-Za-z]+')
        else:
            ret.append('[._+~-]+')
    return re.compile('^' + ''.join(ret))

def tarball_maxver(tbllist, name=None, origversion=None):
    lname = name and name.lower()
    re_verfmt = version_format(origversion)
    tblversions = {}
    for t in tbllist:
        if not (lname and t.filename.lower().startswith(lname)):
            continue
        if RE_BINARY.search(t.filename):
            continue
        match = RE_TARBALL.match(t.filename)
        if not match:
            continue
        ver = match.group(2)
        pfxmatch = (match.group(1) == name)
        vermatch = bool(re_verfmt.match(ver))
        tblversions[(pfxmatch, vermatch, ver)] = t
    if not tblversions:
        return None, None
    pfxmatch, vermatch, ver = max(
        tblversions.keys(), key=lambda x: (x[0], x[1], version_compare_key(x[2])))
    return ver, tblversions[(pfxmatch, vermatch, ver)]

def tag_maxver(taglist, prefix=None, origversion=None):
    versions = {}
    re_verfmt = version_format(origversion)
    for tag in taglist:
        if prefix:
            ver = re.sub('^' + re.escape(prefix) + '[._-]', '',
                         tag.name, flags=re.I)
        ver = RE_VER_PREFIX.sub('', ver)
        if not RE_VERSION.match(ver):
            continue
        versions[(bool(re_verfmt.match(ver)), ver)] = tag
    if not versions:
        return None, None
    vermatch, ver = max(versions.keys(), key=lambda x: (x[0], version_compare_key(x[1])))
    return ver, versions[(vermatch, ver)]

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

def html_select(url, selector, regex):
    req = HSESSION.get(url, timeout=20)
    req.raise_for_status()
    soup = bs4.BeautifulSoup(req.content, 'html5lib')
    tags = soup.select(selector)
    if not tags:
        raise EmptyContent("The selector '%s' for '%s' selected nothing." %
                           (selector, url))
    versions = []
    for x in tags:
        txt = x.get_text().strip()
        match = regex.search(txt)
        if match:
            versions.append(regex.group(1))
    if not versions:
        raise EmptyContent("got nothing in '%s'." % url)
    return max(versions, key=version_compare_key)

def check_github(package, origversion, repo):
    feed = feedparser.parse('https://github.com/%s/releases.atom' % repo)
    tags = []
    for e in feed.entries:
        tag = urllib.parse.unquote(e.link.split('/')[-1])
        evt_time = int(calendar.timegm(e.updated_parsed))
        tags.append(SCMTag(tag, evt_time, e.link))
    ver, tag = tag_maxver(tags, repo.split('/')[-1], origversion)
    if ver:
        return Release(package, 'github', ver, tag.updated, tag.desc)

def check_bitbucket(package, origversion, repo, updtype, prefix):
    if updtype == 'downloads':
        url = 'https://api.bitbucket.org/2.0/repositories/%s/downloads' % repo
    else:
        url = 'https://bitbucket.org/%s/downloads/?tab=tags' % repo
    req = HSESSION.get(url, timeout=20)
    req.raise_for_status()
    if updtype == 'downloads':
        d = req.json()
        tarballs = []
        for row in d['values']:
            tarballs.append(Tarball(
                row['name'], strptime_iso(row['created_on']), None))
        ver, tbl = tarball_maxver(tarballs, prefix, origversion)
        if not ver:
            return None
        url = 'https://bitbucket.org/%s/downloads/' % repo
        return Release(package, 'bitbucket', ver, tbl.updated, url)
    else:
        # the api doesn't sort by time and has multiple pages
        soup = bs4.BeautifulSoup(req.content, 'html5lib')
        tbody = soup.find('div', id='tag-pjax-container').table.tbody
        tags = []
        for tr in tbody.find_all('tr', class_='iterable-item'):
            tag = tr.find('td', class_='name').get_text().strip()
            upd = strptime_iso(tr.find('td', class_='date').time['datetime'])
            tags.append(SCMTag(tag, upd, url))
        ver, tag = tag_maxver(tags, prefix or repo.split('/')[-1], origversion)
        if not ver:
            return None
        return Release(package, 'bitbucket', ver, tag.updated, tag.desc)

def check_gitlab(package, origversion, repo):
    req = HSESSION.get(
        'https://gitlab.com/api/v4/projects/%s/repository/tags' %
        repo.replace('/', '%2F'), timeout=20)
    req.raise_for_status()
    d = req.json()
    tags = []
    for tag in d:
        upd = strptime_iso(tag['commit']['committed_date'])
        tags.append(SCMTag(tag['name'], upd, None))
    ver, tag = tag_maxver(tags, repo.split('/')[-1], origversion)
    if not ver:
        return
    url = 'https://gitlab.com/%s/tags/%s' % (repo, tag.name)
    return Release(package, 'gitlab', ver, tag.updated, url)

def check_pypi(package, origversion, pypiname):
    req = HSESSION.get('https://pypi.python.org/pypi/%s/json' % pypiname, timeout=20)
    req.raise_for_status()
    d = req.json()
    ver = d['info']['version']
    upd = strptime_iso(d['releases'][ver][0]['upload_time'])
    return Release(package, 'pypi', ver, upd, d['info']['release_url'])

def check_rubygems(package, origversion, gemname):
    fetch_time = int(time.time())
    req = HSESSION.get('https://rubygems.org/api/v1/gems/%s.json' % gemname, timeout=20)
    req.raise_for_status()
    d = req.json()
    return Release(package, 'rubygems', d['version'], fetch_time, d['project_uri'])

def check_npm(package, origversion, npmname):
    req = HSESSION.get('https://registry.npmjs.org/%s/' % npmname, timeout=20)
    req.raise_for_status()
    d = req.json()
    ver = d['dist-tags']['latest']
    upd = strptime_iso(d['time'][ver])
    url = 'https://www.npmjs.com/package/' + npmname
    return Release(package, 'npm', ver, upd, url)

def check_cgit(package, origversion, url, project):
    fetch_time = int(time.time())
    req = HSESSION.get(url, timeout=20)
    req.raise_for_status()
    if req.headers.get('Content-Disposition', '').startswith('attachment'):
        return
    elif req.headers.get('Content-Type', '').startswith('application/x'):
        return
    elif len(req.content) > 50*1024*1024:
        raise ValueError('Webpage too large: ' + url)
    soup = bs4.BeautifulSoup(req.content, 'html5lib')
    generatortag = soup.find('meta', attrs={'name': 'generator'})
    tags = []
    links = soup.find_all('a', href=RE_CGIT_TAGS)
    if 'cgit' in generatortag['content']:
        generator = 'cgit'
        for link in links:
            href = link['href']
            ver = href[RE_CGIT_TAGS.search(href).end():]
            span = link.parent.parent.find('span', title=True)
            if not span:
                continue
            upd = strptime_iso(span['title'])
            tags.append(SCMTag(ver, upd, url))
    elif 'gitweb' in generatortag['content']:
        generator = 'gitweb'
        for link in links:
            href = link['href']
            ver = href[RE_CGIT_TAGS.search(href).end():]
            tags.append(SCMTag(ver, fetch_time, url))
    else:
        return
    ver, tag = tag_maxver(tags, project, origversion)
    if not ver:
        return
    return Release(package, generator, ver, tag.updated, url)

def check_launchpad(package, origversion, project):
    req = HSESSION.get('https://api.launchpad.net/1.0/%s/releases' % project, timeout=20)
    req.raise_for_status()
    d = req.json()
    tags = []
    for tag in d['entries']:
        upd = strptime_iso(tag['date_released'])
        tags.append(SCMTag(tag['version'], upd, tag['web_link']))
    ver, tag = tag_maxver(tags, project, origversion)
    if not ver:
        return
    return Release(package, 'launchpad', ver, tag.updated, tag.desc)

def check_sourceforge(package, origversion, project, path, prefix):
    feed = feedparser.parse(
        'https://sourceforge.net/projects/%s/rss?path=%s' % (project, path))
    tarballs = []
    for e in feed.entries:
        filepath = e.title
        evt_time = int(calendar.timegm(e.published_parsed))
        tarballs.append(Tarball(filepath.split('/')[-1], evt_time, e.link))
    ver, tbl = tarball_maxver(tarballs, prefix, origversion)
    if not ver:
        return
    return Release(package, 'sourceforge', ver, tbl.updated, tbl.desc)

def _check_html(package, origversion, url, prefix, content, fetch_time):
    tarballs = []
    for entry in RE_TARBALL_GROUP(prefix).findall(content):
        tarballs.append(Tarball(entry, fetch_time, None))
    ver, tbl = tarball_maxver(tarballs, prefix, origversion)
    if ver:
        return Release(package, 'html', ver, tbl.updated, url)

def check_dirlisting(package, origversion, url, prefix, try_html=True):
    fetch_time = int(time.time())
    req = HSESSION.get(url, stream=True, timeout=20)
    req.raise_for_status()
    bcontent = io.BytesIO()
    if req.headers.get('Content-Disposition', '').startswith('attachment'):
        return
    elif req.headers.get('Content-Type', '').startswith('application/'):
        return
    ctsize = 0
    for chunk in req.iter_content(4096):
        ctsize += len(chunk)
        bcontent.write(chunk)
        if ctsize > 50*1024*1024:
            req.close()
            raise ValueError('Response too large: ' + url)
    content = bcontent.getvalue()
    if ctsize > 1024*1024:
        return _check_html(package, origversion, url, prefix,
            content.decode('utf-8', errors='ignore'), fetch_time)
    soup = bs4.BeautifulSoup(content, 'html5lib')
    try:
        cwd, entries = parse_listing(soup)
    except Exception:
        cwd, entries = None, []
    if entries:
        tarballs = []
        for entry in entries:
            if entry.modified:
                upd = int(calendar.timegm(entry.modified))
            else:
                upd = fetch_time
            tarballs.append(Tarball(entry.name, upd, None))
        ver, tbl = tarball_maxver(tarballs, prefix, origversion)
        if ver:
            return Release(package, 'dirlist', ver, tbl.updated, url)
    if not try_html or prefix is None:
        return
    return _check_html(package, origversion, url, prefix, str(soup), fetch_time)

def check_ftp(package, origversion, url, prefix):
    urlp = urllib.parse.urlparse(url)
    fetch_time = int(time.time())
    with ftputil.FTPHost(urlp.hostname, urlp.username or 'anonymous', urlp.password) as host:
        try:
            st_mtime = int(host.lstat(urlp.path.rstrip('/')).st_mtime)
        except ftputil.error.RootDirError:
            st_mtime = fetch_time
        tarballs = []
        for x in host.listdir(urlp.path):
            tarballs.append(Tarball(x, st_mtime, None))
        ver, tbl = tarball_maxver(tarballs, prefix, origversion)
        if not ver:
            return None
        return Release(package, 'ftp', ver, tbl.updated, url)

def detect_upstream(name, srctype, url, version=None):
    urlp = urllib.parse.urlparse(url)
    if urlp.netloc == 'github.com':
        repo = '/'.join(urlp.path.lstrip('/').split('/')[:2])
        if repo.endswith('.git'):
            repo = repo[:-4]
        return 'github', repo
    elif urlp.netloc == 'gitlab.com':
        repo = '/'.join(urlp.path.lstrip('/').split('/')[:2])
        if repo.endswith('.git'):
            repo = repo[:-4]
        return 'gitlab', repo
    elif urlp.netloc == 'bitbucket.org':
        pathseg = urlp.path.lstrip('/').split('/')
        repo = '/'.join(pathseg[:2])
        if repo.endswith('.git'):
            repo = repo[:-4]
        match = RE_TARBALL.match(urlp.path.split('/')[-1])
        if match:
            prefix = match.group(1)
        else:
            prefix = None
        if len(pathseg) > 2:
            if pathseg[2] == 'downloads':
                return 'bitbucket', repo, 'downloads', prefix
            # pathseg[2] == 'get'
        return 'bitbucket', repo, 'tag', prefix
    elif urlp.netloc in ('pypi.io', 'pypi.python.org'):
        if RE_PYPISRC.match(url):
            pypiname = urlp.path.split('/')[-2]
        else:
            pypiname = urlp.path.split('/')[-1].rsplit('-', 1)[0]
        return 'pypi', pypiname
    elif urlp.netloc in ('rubygems.org', 'gems.rubyforge.org'):
        gemname = RE_TARBALL.match(urlp.path.split('/')[-1]).group(1)
        return 'rubygems', gemname
    elif urlp.netloc == 'registry.npmjs.org':
        projname = urlp.path.strip('/').split('/')[0]
        return 'npm', projname
    elif urlp.netloc == 'launchpad.net':
        projname = urlp.path.strip('/').split('/')[0]
        projnl = projname.lower()
        if name in projnl or projnl in name:
            return 'launchpad', projname
    elif urlp.scheme == 'ftp':
        newurlp = list(urlp)
        newurlp[2], filename = os.path.split(urlp.path)
        if newurlp[2] != '/':
            newurlp[2] += '/'
        if version:
            newurlp[2] = remove_package_version(name, newurlp[2], version)
        newurl = urllib.parse.urlunparse(newurlp)
        fnmatch = RE_TARBALL.match(filename)
        if fnmatch is None:
            return
        return 'ftp', newurl, fnmatch.group(1)
    elif ('cgit' in url or (srctype == 'GITSRC' or 'git' in url)
          and (urlp.netloc in CGIT_SITES or '/snapshot/' in urlp.path)):
        newurlp = list(urlp)
        if urlp.scheme == 'git':
            newurlp[0] = 'http'
        idx = urlp.path.find('/snapshot/')
        if idx != -1:
            newurlp[2] = newurlp[2][:idx+1]
        newurl = urllib.parse.urlunparse(newurlp).split(';')[0]
        if RE_TARBALL.match(newurl.split('/')[-1]):
            return
        project = newurlp[2].rstrip('/')
        if project.endswith('.git'):
            project = project[:-4].rstrip('/')
        project = project.split('/')[-1]
        return 'cgit', newurl, project
    elif srctype != 'SRCTBL':
        return
    elif urlp.scheme in ('http', 'https'):
        newurlp = list(urlp)
        filename = None
        prefix = None
        if not urlp.query:
            ext = os.path.splitext(urlp.path)[1]
            if ext in COMMON_EXT:
                newurlp[2], filename = os.path.split(urlp.path)
                if newurlp[2] != '/':
                    newurlp[2] += '/'
            if filename:
                match = RE_TARBALL.match(filename)
                if match:
                    prefix = match.group(1)
            if urlp.hostname == 'sourceforge.net':
                path = newurlp[2].strip('/').split('/')
                if path[0] == 'projects':
                    filepath = '/' + '/'.join(path[3:])
                    return 'sourceforge', path[1], filepath, prefix
                elif path[0] == 'code-snapshots':
                    return 'sourceforge', path[4], '/', prefix
            elif urlp.hostname in ('downloads.sourceforge.net', 
                'prdownloads.sourceforge.net', 'download.sourceforge.net'):
                path = newurlp[2].strip('/').split('/')
                if path[0] == 'project':
                    filepath = '/' + '/'.join(path[2:])
                    return 'sourceforge', path[1], filepath, prefix
                elif path[0] == 'sourceforge':
                    return 'sourceforge', path[1], '/', prefix
                else:
                    return 'sourceforge', path[0], '/', prefix
            elif urlp.hostname.endswith('.sourceforge.net'):
                return 'sourceforge', urlp.hostname.split('.', 1)[0], '/', prefix
            if version:
                newurlp[2] = remove_package_version(name, newurlp[2], version)
        elif name in urlp.hostname:
            newurlp[2] = '/'
            newurlp[3] = ''
        newurlp[5] = ''
        newurl = urllib.parse.urlunparse(newurlp)
        return 'dirlist', newurl, prefix
    return

def init_db(filename):
    db = sqlite3.connect(filename)
    cur = db.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.execute('CREATE TABLE IF NOT EXISTS upstream_status ('
        'package TEXT PRIMARY KEY,'
        'updated INTEGER,'
        'last_try INTEGER,'
        'err TEXT'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS package_upstream ('
        'package TEXT PRIMARY KEY,'
        'type TEXT,'
        'version TEXT,'
        'time INTEGER,'
        'url TEXT'
    ')')
    db.commit()
    return db

SQL_PACKAGE_SRC = '''
SELECT name, spsrc.key srctype, spsrc.value srcurl, version
FROM v_packages
LEFT JOIN package_spec spsrc
  ON spsrc.package = v_packages.name
  AND spsrc.key IN ('SRCTBL', 'GITSRC', 'SVNSRC', 'BZRSRC')
ORDER BY random()
'''

UPSTRAM_TYPES = {
    'github': check_github,
    'bitbucket': check_bitbucket,
    'gitlab': check_gitlab,
    'pypi': check_pypi,
    'rubygems': check_rubygems,
    'npm': check_npm,
    'cgit': check_cgit,
    'launchpad': check_launchpad,
    'sourceforge': check_sourceforge,
    'dirlist': check_dirlisting,
    'ftp': check_ftp,
}

def check_auto(name, srctype, srcurl, version):
    upstream = detect_upstream(name, srctype, srcurl, version)
    if upstream is None:
        return
    return UPSTRAM_TYPES[upstream[0]](name, version, *upstream[1:])

def check_updates(abbsdbfile, dbfile):
    abbsdb = sqlite3.connect(abbsdbfile)
    pkglist = abbsdb.execute(SQL_PACKAGE_SRC).fetchall()
    db = init_db(dbfile)
    cur = db.cursor()
    now = int(time.time())
    delayed = set(x[0] for x in cur.execute(
        "SELECT package FROM upstream_status "
        "WHERE (last_try + 86400*3 > ? AND (err='not found' OR err LIKE 'HTTPError%')) "
        "OR last_try + 7200 > ?", (now, now)))
    for name, srctype, srcurl, version in pkglist:
        if not srctype or name in delayed:
            continue
        fetch_time = int(time.time())
        upstream = detect_upstream(name, srctype, srcurl, version)
        logging.info('%s: %r' % (name, upstream))
        if not upstream:
            cur.execute(
                'INSERT OR IGNORE INTO upstream_status(package) VALUES (?)', (name,))
            cur.execute(
                'UPDATE upstream_status SET last_try=? AND err=? WHERE package=?',
                (fetch_time, "can't detect upstream", name))
            logging.warning("%s: can't detect upstream" % name)
            continue
        try:
            release = UPSTRAM_TYPES[upstream[0]](name, version, *upstream[1:])
            err = None if release else 'not found'
        except Exception as ex:
            err = type(ex).__name__ + ': ' + str(ex)
            logging.exception('%s update failed' % name)
        cur.execute(
            'INSERT OR IGNORE INTO upstream_status(package) VALUES (?)', (name,))
        cur.execute(
            'UPDATE upstream_status SET last_try=?, err=? WHERE package=?',
            (fetch_time, err, name))
        if not err:
            cur.execute('UPDATE upstream_status SET updated=? WHERE package=?',
                (fetch_time, name))
            # print(release)
            cur.execute('REPLACE INTO package_upstream VALUES (?,?,?,?,?)', release)
        db.commit()
        gc.collect()
    cur.execute('PRAGMA optimize')

SQL_VIEW_PISS_VERSION = '''
CREATE VIEW IF NOT EXISTS v_package_upstream AS
SELECT
  package, coalesce(pu.version, ap.latest_version) version,
  coalesce(pu.time, ap.updated_on) updated,
  coalesce(pu.url, ('https://release-monitoring.org/project/' || ap.id || '/')) url
FROM (
  SELECT package FROM package_upstream
  UNION
  SELECT package FROM anitya_link
) p
LEFT JOIN package_upstream pu USING (package)
LEFT JOIN anitya_link al USING (package)
LEFT JOIN anitya_projects ap ON al.projectid=ap.id
'''

def main(argv):
    parser = argparse.ArgumentParser(description='Projects Information Storage System')
    parser.add_argument('abbsdb', help='abbs-meta database file')
    parser.add_argument('db', help='PISS database file')
    args = parser.parse_args(argv)

    logging.info('Checking updates...')
    check_updates(args.abbsdb, args.db)
    logging.info('Checking anitya updates...')
    try:
        anitya.update_db(args.db, args.abbsdb)
    except Exception:
        logging.exception('Anitya update failed.')
    db = sqlite3.connect(args.db)
    db.execute(SQL_VIEW_PISS_VERSION)
    db.execute('VACUUM')
    db.commit()
    logging.info('Done.')
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
