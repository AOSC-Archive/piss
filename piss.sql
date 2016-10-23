PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS packages (
    name TEXT PRIMARY KEY,  -- coreutils
    category TEXT,  -- base
    section TEXT,  -- utils
    pkg_section TEXT,  -- (PKGSEC)
    version TEXT,  -- 8.25
    release TEXT,  -- None
    description TEXT
);
CREATE TABLE IF NOT EXISTS package_spec (
    package TEXT,
    key TEXT,
    value TEXT,
    PRIMARY KEY (package, key),
    FOREIGN KEY(package) REFERENCES packages(name)
);
CREATE TABLE IF NOT EXISTS package_dependencies (
    package TEXT,
    dependency TEXT,
    version TEXT,
    -- PKGDEP, PKGRECOM, PKGBREAK, PKGCONFL, PKGREP, BUILDDEP
    relationship TEXT,
    PRIMARY KEY (package, dependency, relationship),
    FOREIGN KEY(package) REFERENCES packages(name)
    -- we may have unmatched dependency package name
    -- FOREIGN KEY(dependency) REFERENCES packages(name)
);
CREATE INDEX IF NOT EXISTS idx_package_dependencies
    ON package_dependencies (package);
CREATE TABLE IF NOT EXISTS package_version (
    package TEXT,
    source TEXT, -- upstream, abbs, deb
    arch TEXT,
    version TEXT,
    FOREIGN KEY(package) REFERENCES packages(name)
);
CREATE TABLE IF NOT EXISTS upstreams (
    name TEXT PRIMARY KEY,
    type TEXT,
    url TEXT,
    branch TEXT,
    FOREIGN KEY(package) REFERENCES packages(name)
);
CREATE TABLE IF NOT EXISTS package_upstream (
    package TEXT PRIMARY KEY,
    upstream TEXT,
    FOREIGN KEY(package) REFERENCES packages(name),
    FOREIGN KEY(upstream) REFERENCES upstreams(name)
);
CREATE TABLE IF NOT EXISTS upstream_update (
    upstream TEXT,
    category TEXT, -- commit, issue, pr, tag, release, news
    time INTEGER,
    subscription INTEGER,
    title TEXT,
    content TEXT,
    url TEXT UNIQUE,
    FOREIGN KEY(upstream) REFERENCES upstreams(name),
    FOREIGN KEY(subscription) REFERENCES upstream_subscription(id)
);
CREATE TABLE IF NOT EXISTS upstream_subscription (
    id INTEGER PRIMARY KEY,
    upstream TEXT,
    type TEXT, -- feed, email
    category TEXT, -- all, upstream_update.category
    url TEXT,
    last_update INTEGER,
    FOREIGN KEY(upstream) REFERENCES upstreams(name)
);
CREATE TABLE IF NOT EXISTS pakreq (
    package TEXT PRIMARY KEY,
    description TEXT,
    url TEXT,
    resolution TEXT
);
