#!/bin/bash

PISS_SCRIPT=piss.py
DATABASE=piss.db
CHORES=chores.yaml
TITLE="PISS Updates"
SUBTITLE="New packaging tasks"
ATOM_ID="pissnews"
ATOM_LINK="https://example.com/feed.cgi"
ATOM_LANG="en"
NUMBER=100

echo 'Status: 200 OK'
echo 'Content-Type: application/atom+xml; charset=utf-8'
echo

python3 PISS_SCRIPT check -d "$DATABASE" -f atom -t "$TITLE" -s "$SUBTITLE" -i "$ATOM_ID" -l "$ATOM_LINK" -L "$ATOM_LANG" -n "$NUMBER" - 2> /dev/null

