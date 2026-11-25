---
evidence:
  - articles: [accounts, opportunities, people, calls, knowledge]   # each collection's articles, as their text
    text_only: true
    keep: "What each article is: its name and the line that says what it is."   # loads only those lines, so the evidence stays small
    top: 80
rendered:                             # the wiki writes Contents itself, with no model
  Contents: { kind: collections, style: outline, show: [count] }   # style: outline | headings; show: count, description
heartbeat: [change]                   # new articles and facts wait out the workspace's schedule.articles, 4h by default
---

The index of this wiki: the first article a reader opens.

The wiki writes Contents itself. Under each other heading, list at most
five of that collection's most notable articles, each a bullet that links the
article, such as [Halcyon Mills](../accounts/halcyon-mills.md), then a dash and a few
plain words on what it is. Keep the citation the article gives the words you
take, such as [^fact_01m7p4qga4pxpjkgvf]. Leave out the heading of a collection that has
no articles yet: a line that says so cites nothing, so it would not land.

# Output

## Contents

## Accounts

## Opportunities

## People

## Calls

## Knowledge
