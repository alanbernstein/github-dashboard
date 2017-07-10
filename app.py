from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import column
from sqlalchemy.sql import func
from github import Github
import datetime
from datetime import datetime as dt
import collections
import bisect
from math import ceil

# Config #############################
import configparser
Config = configparser.ConfigParser()
Config.read("pilosa.cfg")
section = "App"
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
    db_args = (Config.get(section, 'username'),
               Config.get(section, 'password'),
               Config.get(section, 'hostname'),
               Config.get(section, 'database'),
    )
    db_url = 'postgresql://%s:%s@%s/%s' % db_args

    print(db_url)


# TODO heroku
# http://blog.y3xz.com/blog/2012/08/16/flask-and-postgresql-on-heroku


# Init stuff ###########################
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = 'users'
    github_id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True)
    starred_time = db.Column(db.DateTime)
    forked_time = db.Column(db.DateTime)
    watched_time = db.Column(db.DateTime)

    def __init__(self, github_id):
        self.github_id = github_id

    def __repr__(self):
        s = []
        if self.starred_time:
            s.append('starred at %s' % self.starred_time)
        if self.forked_time:
            s.append('forked at %s' % self.forked_time)
        if self.watched_time:
            s.append('watched at %s' % self.watched_time)
        return '[User %9s: %s]' % (self.github_id, ', '.join(s))


def get_or_create_user(id):
    """
    input: id
    output:  (user, created_flag)
    if user with id exists, return that user
    if not exists, create new user and return it
    """
    result = User.query.filter_by(github_id=id)
    if result.count() == 0:
        return User(id), True
    else:
        return result.first(), False


gh = Github(github_token)
repo = gh.get_repo(github_repo)


def init_db():
    db.create_all()


# API retrieve functions ###############
def get_and_cache_stargazers():
    stargazers = repo.get_stargazers_with_dates()
    new = 0
    for n, s in enumerate(stargazers):
        user, created = get_or_create_user(s.user.id)
        user.username = s.user.login
        user.starred_time = s.starred_at
        if created:
            db.session.add(user)
            new += 1
    print('%d users retrieved, %d new' % (n, new))
    db.session.commit()


def get_and_cache_forks():
    forks = repo.get_forks()
    new = 0
    for n, f in enumerate(forks):
        user, created = get_or_create_user(f.owner.id)
        user.username = f.owner.login
        user.forked_time = f.created_at
        if created:
            db.session.add(user)
            new += 1
    print('%d users retrieved, %d new' % (n, new))
    db.session.commit()


def get_and_cache_watchers():
    # what are now called stargazers in the GUI used to be watchers.
    # so in the backward-compatible API, watchers = stargazrs.
    # what are now called watchers in the GUI are subscribers in the API.
    # so to get watchers, look at subscribers.
    #
    # Also, watch time is not available throught the API, so we
    # can only track it by checking the API periodically.
    # that's why this function uses current time, and why it only
    # updates the database for new watchers.
    watchers = repo.get_subscribers()
    time = dt.now()
    time = time.replace(hour=0, minute=0, second=0, microsecond=0)
    new = 0
    for n, w in enumerate(watchers):
        user, created = get_or_create_user(w.id)
        if not created:
            continue
        user.username = w.login
        user.watched_time = time
        db.session.add(user)
        new += 1
    print('%d users retrieved, %d new' % (n, new))
    db.session.commit()


# DB read functions #####################
def get_date_history(field, timespan):
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

    # TODO use ORM for grouping?
    # TODO use ORM to label column - thought query((column(field).label('time'))) would work?

    q = (User.query.filter(column(field).isnot(None))
         .order_by(column(field)))

    rows = q.all()
    agg = group_time_rows(rows, seconds, field)
    return [{'period': dt.strftime(k, fmt),
             'count': len(v)} for k, v in agg.items()]


def group_time_rows(rows, seconds, field):
    # TODO: want to include 0 counts so horizontal axis is right (would make data sparse though)
    interval = datetime.timedelta(seconds=seconds)
    start = rows[0].__getattribute__(field)
    # start = rows[0].time
    end = dt.now()
    N = (end - start).total_seconds() / seconds
    grid = [start + n*interval for n in range(int(ceil(N))+1)]
    bins = collections.OrderedDict()

    for row in rows:
        id = row.github_id
        time = row.__getattribute__(field)
        # time = row.time
        idx = bisect.bisect(grid, time)
        if grid[idx] in bins:
            bins[grid[idx]].append(id)
        else:
            bins[grid[idx]] = [id]

    return bins


# Endpoints ########################
@app.route('/users/list')
def list_all_users():
    m = ''
    for n, u in enumerate(User.query.all()):
        m += '%4d %s\n' % (n, u)
    m = '<pre>%s</pre>' % m
    return m


# TODO? refactor this so the function argument is data, not a string
@app.route('/forks/history/<string:timespan>/')
def aggregate_forks_graph(timespan=None):
    return aggregate_graph('forked_time', 'Forks', timespan)


@app.route('/stars/history/<string:timespan>/')
def aggregate_stargazers_graph(timespan=None):
    return aggregate_graph('starred_time', 'Stars', timespan)


@app.route('/watchers/history/<string:timespan>/')
def aggregate_watchers_graph(timespan=None):
    return aggregate_graph('watched_time', 'Watchers', timespan)


def aggregate_graph(column, noun, timespan=None):
    """
    grouping is determined by timespan
    timespan can be "hour", "day", "week", or integer number of seconds.
    """
    timespan = timespan or 'day'

    title = '%s per %s' % (noun, timespan)

    history = get_date_history(column, timespan)
    values = [row['count'] for row in history]
    labels = [row['period'] for row in history]

    return render_template('linegraph.html',
                           values=values,
                           labels=labels,
                           label=column,
                           ymax=max(values),
                           title=title)


@app.route('/<string:what>/retrieve/')
def get_github_data(what=None):
    """call all API read functions"""

    what = what or 'users'

    if what in ['stars', 'stargazers', 'users']:
        print('getting stargazers...')
        get_and_cache_stargazers()
    if what in ['forks', 'users']:
        print('getting forks...')
        get_and_cache_forks()
    if what in ['watchers', 'users']:
        print('getting watchers...')
        get_and_cache_watchers()
    m = 'read data from github api, stored in cache'
    print(m)
    return m


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/init')
def init():
    try:
        init_db()
    except Exception as exc:
        return 'error: %s' % exc

    return 'initialized database'


@app.route('/debug')
def debug():
    field = 'watched_time'
    qq = (User.query.filter(column(field).isnot(None))
          .order_by(column(field)))

    rows = qq.all()
    import ipdb; ipdb.set_trace()
    return "debug'd"


# Start #######################
if __name__ == "__main__":
    app.run(host=host, port=port)
