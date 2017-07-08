from flask import Flask, g, render_template
from github import Github
import psycopg2
import datetime
from datetime import datetime as dt
import collections
import bisect
from math import ceil

# Config #############################
import ConfigParser
Config = ConfigParser.ConfigParser()
Config.read("pilosa.cfg")
section = "Base"
if section not in Config.sections():
    host = "127.0.0.1"
    port = 5000
else:
    host = Config.get(section, 'host')
    port = Config.get(section, 'port')

section = "Github"
if section not in Config.sections():
    raise Exception
else:
    github_token = Config.get(section, 'token')
    github_repo = Config.get(section, 'repo')

section = "Postgres"
if section not in Config.sections():
    raise Exception
else:
    db_args = (Config.get(section, 'database'),
               Config.get(section, 'username'),
               Config.get(section, 'password'),
               Config.get(section, 'hostname'),
    )
    db_config = "dbname='%s' user='%s' password='%s' host='%s'" % db_args
    print(db_config)



# Init stuff ###########################
app = Flask(__name__)
gh = Github(github_token)
repo = gh.get_repo(github_repo)


def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource('schema.sql', mode='r') as f:
            db.cursor().executescript(f.read())
        db.commit()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg2.connect(db_config)
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# API retrieve functions ###############
def get_and_cache_stargazers():
    stargazers = repo.get_stargazers_with_dates()
    db = get_db()
    cur = db.cursor()

    for n, s in enumerate(stargazers):
        query = "INSERT INTO star_cache (github_id, time) values (%s, '%s') ON CONFLICT DO NOTHING"
        cur.execute(query % (s.user.id, s.starred_at))
    db.commit()


def get_and_cache_forks():
    forks = repo.get_forks()
    db = get_db()
    cur = db.cursor()
    for f in forks:
        query = "INSERT INTO fork_cache (github_id, time) values (%s, '%s') ON CONFLICT DO NOTHING"
        cur.execute(query % (f.owner.id, f.created_at))
    db.commit()


def get_and_cache_watchers():
    # what are now called stargazers in the GUI used to be watchers
    # so in the backward-compatible API, watchers = stargazrs
    # what are now called watchers in the GUI are subscribers in the API
    # so to get watchers, look at subscribers
    watchers = repo.get_subscribers()
    db = get_db()
    cur = db.cursor()
    date = dt.now()
    date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    for n, w in enumerate(watchers):
        query = "INSERT INTO watch_cache (github_id, time) VALUES (%s, '%s') ON CONFLICT DO NOTHING"
        cur.execute(query % (w.id, date))
    db.commit()


# DB read functions #####################
def get_stargazers_from_cache():
    db = get_db()
    cur = db.cursor()
    query = "SELECT github_id, time from user_cache"
    cur.execute(query)
    rows = cur.fetchall()
    return [{'github_id': r[0], 'time': r[1]} for r in rows]


def get_date_history(table, timespan):
    # compute #thing vs date from cached data

    if 'hour' in timespan:
        seconds = 60 * 60
        fmt = '%Y/%m/%d %H'
    elif 'day' in timespan:
        seconds = 60 * 60 * 24
        fmt = '%Y/%m/%d'
    elif 'week' in timespan:
        seconds = 60 * 60 * 24 * 7
        fmt = '%Y/%m/%d'
    else:
        seconds = int(timespan)
        fmt = '%Y/%m/%d %H'

    db = get_db()
    cur = db.cursor()
    query = "SELECT * FROM %s" % table
    cur.execute(query)
    rows = cur.fetchall()

    agg = group_time_rows(rows, seconds)
    return [{'period': dt.strftime(k, fmt),
             'count': len(v)} for k, v in agg.items()]


def group_time_rows(rows, seconds):
    # TODO: want to include 0 counts so horizontal axis is right (would make data sparse though)
    interval = datetime.timedelta(seconds=seconds)
    rows.sort(key=lambda x: x[1])
    start = rows[0][1]
    end = dt.now()
    N = (end - start).total_seconds() / seconds
    grid = [start + n*interval for n in range(int(ceil(N))+1)]
    bins = collections.OrderedDict()

    for id, date in rows:
        idx = bisect.bisect(grid, date)
        if grid[idx] in bins:
            bins[grid[idx]].append(id)
        else:
            bins[grid[idx]] = [id]

    return bins


# Endpoints ########################
@app.route('/stars/list')
def list_stargazers():
    m = ''
    for n, s in enumerate(get_stargazers_from_cache()):
        m += '%4d %s %s<br>\n' % (n, s['time'], s['github_id'])
    return m


# TODO? refactor this so the function argument is data, not a string
@app.route('/forks/history/<string:timespan>/')
def aggregate_forks_graph(timespan=None):
    return aggregate_graph('fork_cache', 'Forks', timespan)


@app.route('/stars/history/<string:timespan>/')
def aggregate_stargazers_graph(timespan=None):
    return aggregate_graph('star_cache', 'Stars', timespan)


@app.route('/watchers/history/<string:timespan>/')
def aggregate_watchers_graph(timespan=None):
    return aggregate_graph('watch_cache', 'Watchers', timespan)


def aggregate_graph(table, noun, timespan=None):
    """
    grouping is determined by timespan
    timespan can be "hour", "day", "week", or integer number of seconds.
    """
    timespan = timespan or 'day'

    title = '%s per %s' % (noun, timespan)

    history = get_date_history(table, timespan)
    values = [row['count'] for row in history]
    labels = [row['period'] for row in history]

    return render_template('linegraph.html',
                           values=values,
                           labels=labels,
                           label=table,
                           ymax=max(values),
                           title=title)


@app.route('/retrieve')
def get_github_data():
    """call all API read functions"""

    print('getting stargazers...')
    get_and_cache_stargazers()
    print('getting forks...')
    get_and_cache_forks()
    print('getting watchers...')
    get_and_cache_watchers()
    m = 'read data from github api, stored in cache'
    print(m)
    return m


@app.route('/')
def index():
    return render_template('index.html')


# Start #######################
if __name__ == "__main__":
    app.run(host=host, port=port)
