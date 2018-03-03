#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import sqlite3
import logging
import argparse
import calendar
import warnings
import functools
import collections
import urllib.parse

import anitya
from htmllistparse import parse as parse_listing

import bs4
import ftputil
import requests
import dateutil.parser

__version__ = '1.0'

logging.basicConfig(
    format='[%(levelname)s] %(message)s', level=logging.INFO)
logging.captureWarnings(True)

USER_AGENT = 'Mozilla/5.0 (compatible; PISS/%s; +https://github.com/AOSC-Dev/piss)' % __version__

RE_ALL_DIGITS_OR_NOT = re.compile("\d+|\D+")
RE_DIGITS = re.compile("\d+")
RE_ALPHA = re.compile("[A-Za-z]")
RE_SRCHOST = re.compile(r'^https://(github\.com|bitbucket\.org|gitlab\.com)')
RE_PYPI = re.compile(r'^https?://pypi\.(python\.org|io)')
RE_PYPISRC = re.compile(r'^https?://pypi\.(python\.org|io)/packages/source/')
RE_VER_PREFIX = re.compile(r'^(?:version|ver|v|release|rel|r)', re.I)
RE_TARBALL = re.compile(r'^(.+)[._-][vr]?(\d.*?)(?:[.-_](?:orig|src))?(\.tar\.xz|\.tar\.bz2|\.tar\.gz|\.t.z|\.zip)$', re.I)
RE_TARBALL_GROUP = lambda s: re.compile(r'\b(' + re.escape(s) + r'[._-][vr]?(?:\d.*?)(?:[.-_](?:orig|src))?(?:\.tar\.xz|\.tar\.bz2|\.tar\.gz|\.t.z|\.zip))\b', re.I)
RE_BINARY = re.compile('(linux32|linux64|win32|win64|osx|x86|i.86|x64|amd64|arm64|armhf|armel|mips|ppc|powerpc|s390x)', re.I)
RE_VER_MINOR = re.compile(r'\d+\.\d+$')

COMMON_EXT = frozenset(('.gz', '.bz2', '.xz', '.tar', '.7z', '.rar', '.zip', '.tgz', '.tbz', '.txz'))


strptime_iso = lambda s: int(dateutil.parser.parse(s).timestamp())

HSESSION = requests.Session()
HSESSION.headers['User-Agent'] = USER_AGENT

class Release(collections.namedtuple(
    'Release', 'package upstreamtype version updated url')):
    def __new__(cls, package, upstreamtype, version, updated, url):
        ver = RE_VER_PREFIX.sub('', version)
        ver = re.sub('^' + re.escape(package) + '[._-]', '', ver)
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

def tarball_maxver(tbllist, name=None):
    lname = name and name.lower()
    tblversions = {}
    for t in tbllist:
        if not (lname and t.filename.lower().startswith(lname)):
            continue
        if RE_BINARY.match(t.filename):
            continue
        match = RE_TARBALL.match(t.filename)
        if not match:
            continue
        tblversions[match.group(2)] = t
    if not tblversions:
        return None, None
    ver = max(tblversions.keys(), key=version_compare_key)
    return ver, tblversions[ver]

def tag_maxver(taglist):
    versions = {}
    for tag in taglist:
        ver = RE_VER_PREFIX.sub('', tag.name)
        versions[ver] = tag
    if not versions:
        return None, None
    ver = max(versions.keys(), key=version_compare_key)
    return ver, versions[ver]

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
    req = HSESSION.get(url, timeout=30)
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

def check_github(package, repo):
    # FIXME: use atom feed to bypass rate limit
    fetch_time = int(time.time())
    req = HSESSION.get(
        'https://api.github.com/repos/%s/tags' % repo, timeout=30)
    req.raise_for_status()
    d = req.json()
    tags = []
    for row in d:
        tags.append(SCMTag(row['name'], fetch_time, row['commit']['url']))
    ver, tag = tag_maxver(tags)
    url = 'https://github.com/%s/releases' % repo
    return Release(package, 'github', ver, fetch_time, url)

def check_bitbucket(package, repo, updtype):
    if updtype == 'downloads':
        url = 'https://api.bitbucket.org/2.0/repositories/%s/downloads' % repo
    else:
        url = 'https://api.bitbucket.org/2.0/repositories/%s/refs/tags' % repo
    req = HSESSION.get(url, timeout=30)
    req.raise_for_status()
    d = req.json()
    if updtype == 'downloads':
        tarballs = []
        for row in d['values']:
            tarballs.append(Tarball(
                row['name'], strptime_iso(row['created_on']), None))
        ver, tbl = tarball_maxver(tarballs)
        if not ver:
            return None
        url = 'https://bitbucket.org/%s/downloads/' % repo
        return Release(package, 'bitbucket', ver, tbl.updated, url)
    else:
        tags = []
        for row in d['values']:
            tags.append(SCMTag(
                row['name'], strptime_iso(row['date']), row['links']['html']['href']))
        ver, tag = tag_maxver(tags)
        if not ver:
            return None
        return Release(package, 'bitbucket', ver, tag.updated, tag.desc)

def check_gitlab(package, repo):
    req = HSESSION.get(
        'https://gitlab.com/api/v4/projects/%s/repository/tags' %
        repo.replace('/', '%2F'), timeout=30)
    req.raise_for_status()
    d = req.json()
    tags = []
    for tag in d:
        upd = strptime_iso(tag['commit']['committed_date'])
        tags.append(SCMTag(tag['name'], upd, None))
    ver, tag = tag_maxver(tags)
    url = 'https://gitlab.com/%s/tags/%s' % (repo, tag.name)
    return Release(package, 'gitlab', ver, tag.updated, url)

def check_pypi(package, pypiname):
    req = HSESSION.get('https://pypi.python.org/pypi/%s/json' % pypiname, timeout=30)
    req.raise_for_status()
    d = req.json()
    ver = d['info']['version']
    upd = strptime_iso(d['releases'][ver][0]['upload_time'])
    return Release(package, 'pypi', ver, upd, d['info']['release_url'])

def check_launchpad(package, project):
    req = HSESSION.get('https://api.launchpad.net/1.0/%s/releases' % project, timeout=30)
    req.raise_for_status()
    d = req.json()
    tags = []
    for tag in d['entries']:
        upd = strptime_iso(tag['date_released'])
        tags.append(SCMTag(tag['version'], upd, tag['web_link']))
    ver, tag = tag_maxver(tags)
    return Release(package, 'launchpad', ver, tag.updated, tag.desc)

def check_sourceforge(package, project, path, prefix):
    return
    url = 'https://sourceforge.mirrorservice.org/%s/%s/%s/%s' % (
        project[0], project[:2], project, path.lstrip('/'))
    return check_dirlisting(package, url, prefix, False)

def check_dirlisting(package, url, prefix, try_html=True):
    fetch_time = int(time.time())
    req = HSESSION.get(url, timeout=30)
    req.raise_for_status()
    if len(req.content) > 50*1024*1024:
        raise ValueError('Webpage too large: ' + url)
    soup = bs4.BeautifulSoup(req.content, 'html5lib')
    cwd, entries = parse_listing(soup)
    tarballs = []
    for entry in entries:
        if entry.modified:
            upd = int(calendar.timegm(entry.modified))
        else:
            upd = fetch_time
        tarballs.append(Tarball(entry.name, upd, None))
    ver, tbl = tarball_maxver(tarballs, prefix)
    if ver:
        return Release(package, 'dirlist', ver, tbl.updated, url)
    if not try_html or prefix is None:
        return
    tarballs = []
    for entry in RE_TARBALL_GROUP(prefix).findall(str(soup)):
        tarballs.append(Tarball(entry, fetch_time, None))
    ver, tbl = tarball_maxver(tarballs, prefix)
    if ver:
        return Release(package, 'html', ver, tbl.updated, url)

def check_ftp(package, url, prefix):
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
        ver, tbl = tarball_maxver(tarballs, prefix)
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
        if len(pathseg) > 2:
            if pathseg[2] == 'downloads':
                return 'bitbucket', repo, 'downloads'
            # pathseg[2] == 'get'
        return 'bitbucket', repo, 'tag'
    elif urlp.netloc in ('pypi.io', 'pypi.python.org'):
        if RE_PYPISRC.match(url):
            pypiname = urlp.path.split('/')[-2]
        else:
            pypiname = urlp.path.split('/')[-1].rsplit('-', 1)[0]
        return 'pypi', pypiname
    elif urlp.netloc == 'launchpad.net':
        projname = urlp.path.strip('/').split('/')[0]
        projnl = projname.lower()
        if name in projnl or projnl in name:
            return 'launchpad', projname
    elif urlp.scheme == 'ftp':
        newurlp = list(urlp)
        filename = None
        if urlp.path[-1] != '/':
            newurlp[2], filename = os.path.split(urlp.path)
            if newurlp[2] != '/':
                newurlp[2] += '/'
        if version:
            newurlp[2] = remove_package_version(name, newurlp[2], version)
        newurl = urllib.parse.urlunparse(newurlp)
        fnmatch = RE_TARBALL.match(urlp.path.split('/')[-1])
        return 'ftp', newurl, fnmatch.group(1)
    elif urlp.path.rstrip('/').endswith('.git') or srctype != 'SRCTBL':
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
    'launchpad': check_launchpad,
    'sourceforge': check_sourceforge,
    'dirlist': check_dirlisting,
    'ftp': check_ftp,
}

def check_updates(abbsdbfile, dbfile):
    abbsdb = sqlite3.connect(abbsdbfile)
    pkglist = abbsdb.execute(SQL_PACKAGE_SRC).fetchall()
    db = init_db(dbfile)
    cur = db.cursor()
    delayed = set(x[0] for x in cur.execute(
        'SELECT package FROM upstream_status WHERE last_try + 7200 > ?',
        (int(time.time()),)))
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
            release = UPSTRAM_TYPES[upstream[0]](name, *upstream[1:])
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
    cur.execute('PRAGMA optimize')

def main(argv):
    parser = argparse.ArgumentParser(description='Projects Information Storage System')
    parser.add_argument('abbsdb', help='abbs-meta database file')
    parser.add_argument('db', help='PISS database file')
    args = parser.parse_args(argv)

    check_updates(args.abbsdb, args.db)
    anitya.update_db(args.db)
    return 0

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
