from Cookie import SimpleCookie
from datetime import datetime, timedelta
from os.path import join, dirname
import random

from itty import get, post, run_itty
import itty
from jinja2 import Environment, FileSystemLoader
import typd


env = Environment(loader=FileSystemLoader(join(dirname(__file__), 'templates')))

settings = {}

t = typd.TypePad(endpoint='http://api.typepad.com/')

cache = dict()


def configure():
    if 'memcached_servers' in settings:
        class Cache(object):
            def __init__(self, cache):
                self.cache = cache
            def __getitem__(self, key):
                return self.cache.get(key)
            def __setitem__(self, key, value):
                return self.cache.set(key, value)
            def get(self, key, default=None):
                ret = self.cache.get(key)
                if ret is not None:
                    return ret
                return default
            def get_multi(self, keys, key_prefix=''):
                return self.cache.get_multi(keys, key_prefix=key_prefix)
            def set_multi(self, data, key_prefix=''):
                return self.cache.set_multi(data, key_prefix=key_prefix)

        import memcache
        global cache
        cache = Cache(memcache.Client(settings['memcached_servers'], debug=10))
    else:
        class Cache(dict):
            def get_multi(self, keys, key_prefix=''):
                return dict((k, self[key_prefix + str(k)]) for k in keys if key_prefix + str(k) in self)
            def set_multi(self, data, key_prefix=''):
                self.update((key_prefix + str(k), v) for k, v in data.items())

        cache = Cache()


def render(request, templatename, data, extrastyle=None):
    if extrastyle is None:
        try:
            cookieheader = request._environ['HTTP_COOKIE']
        except KeyError:
            pass
        else:
            c = SimpleCookie()
            c.load(cookieheader)
            if 'style' in c:
                extrastyle = c['style'].value

    t = env.get_template(templatename)
    return t.render(rot=random_rotation(),
        ganalytics_code=settings.get('ganalytics_code'),
        extrastyle=extrastyle,
        **data)


def random_rotation():
    while True:
        yield random.gauss(0, 3)


@get('/favicon.ico')
def favicon(request):
    raise itty.Redirect('http://www.typepad.com/favicon.ico')


@get('/static/(?P<filename>.+)')
def static(request, filename):
    return itty.serve_static_file(request, filename, root=join(dirname(__file__), 'static'))


@get('/')
def index(request):
    if 'consumer_key' in settings:
        raise itty.Redirect('http://www.typepad.com/services/api-redirect-identify?consumer_key=%s&nonce=7'
            % settings['consumer_key'])

    try:
        profilename = request.GET['name']
    except KeyError:
        return render(request, 'index.html', {})
    raise itty.Redirect('/' + profilename)


@get('/.services/tp-session')
def identify_user(request):
    user = request.GET.get('user')
    if user:
        userobj = t.users.get(user)
        raise itty.Redirect('/' + userobj.preferred_username)
    raise itty.Redirect('http://www.typepad.com/services/signin?to=http://leapf.org/')


def add_followers(profilename, notes):
    cachekey = '%s:follow' % profilename
    followers = set(cache.get(cachekey, ()))

    # Yield the followers first so we can consult it later.
    yield followers

    for note in notes:
        followers.add(note.actor.url_id)
        yield note

    cache[cachekey] = tuple(followers)


def good_notes_for_notes(notes):
    for note in notes:
        # TODO: skip notes when paging

        if note.verb in ('AddedNeighbor', 'SharedBlog', 'JoinedGroup'):
            continue

        obj = note.object

        if obj is None:  # deleted asset
            continue
        if obj.permalink_url is None:  # no ancillary
            continue
        if obj.source is not None:  # no boomerang
            if obj.source.by_user:
                continue
        if obj.container is not None and obj.container.url_id in ('6p0120a5e990ac970c', '6a013487865036970c0134878650f2970c'):
            continue

        if note.verb == 'NewAsset':
            if getattr(obj, 'root', None) is not None:
                note.original = obj
                note.verb = 'Comment'
                obj = note.object = t.assets.get(obj.root.url_id)

            if getattr(obj, 'reblog_of', None) is not None:
                note.original = obj
                note.verb = 'Reblog'

        if note.verb == 'NewAsset':
            okay_types = ['Post']
            if obj.container and obj.container.object_type == 'Group':
                okay_types.extend(['Photo', 'Audio', 'Video', 'Link'])
            if obj.object_type not in okay_types:
                continue

        # Move all reactions up to the root object of reblogging.
        while getattr(obj, 'reblog_of', None) is not None:
            obj = note.object = t.assets.get(obj.reblog_of.url_id)

        # Yay, let's show this one!
        yield note


def objs_for_notes(notes, followers=None, profilename=None):
    interesting = dict()

    for note in notes:
        obj = note.object

        try:
            objdata = interesting[obj.url_id]
        except KeyError:
            objdata = {
                'object': obj,
                'actions': list(),
                'when': note.published,
                #'action_times': ...?
            }
            interesting[obj.url_id] = objdata
        else:
            # Date the object by its oldest (last) event.
            objdata['when'] = note.published

        if note.verb == 'NewAsset':
            # Skip the whole object if the post was backdated (the asset's publish time is out of line with the event time).
            if abs(note.published - obj.published) > timedelta(days=1):
                objdata['SKIP'] = True

            objdata['new_asset'] = True
        else:
            objdata['actions'].append(note)

    # Only consider objects that are not known to have been pinned to other times already.
    key_prefix = 'whenevent:%s:' % profilename
    whentimes = cache.get_multi(interesting.keys(), key_prefix=key_prefix)
    for url_id in list(interesting.keys()):
        eventwhen = interesting[url_id]['when']
        if url_id not in whentimes:
            whentimes[url_id] = eventwhen
        elif whentimes[url_id] > eventwhen:
            whentimes[url_id] = eventwhen
        elif whentimes[url_id] < eventwhen:
            interesting[url_id]['SKIP'] = True
    cache.set_multi(whentimes, key_prefix=key_prefix)

    for objdata in sorted(interesting.values(), key=lambda d: d['when'], reverse=True):
        if objdata.get('SKIP'):
            continue

        obj = objdata['object']
        obj.actions = objdata['actions']
        if not objdata.get('new_asset'):
            # If we don't have a NewAsset event but we know the asset is by
            # someone we follow, don't show the asset. The NewAsset event just
            # passed out of the window. (Since we already went through all the
            # notes, the followers list is up to date.)
            if followers is not None and obj.author.url_id in followers:
                continue
            if profilename is not None and obj.author.preferred_username == profilename:
                continue
            obj.why = obj.actions[-1]
        yield obj


@get('/.customize')
def customize(request):
    return render(request, 'customize.html', {}, extrastyle=False)


@post('/.customize')
def customize(request):
    # Set a cookie?
    c = SimpleCookie()
    c['style'] = request.POST.get('url', '')
    expires = datetime.now() + timedelta(weeks=520)
    c['style']['expires'] = expires.strftime("%a, %d-%b-%Y %H:%M:%S PST")
    cookieheaders = c.output()

    headers = [
        ('Location', '/'),
    ]
    headers.extend(header.split(':', 1) for header in cookieheaders.split('\n'))

    return itty.Response('/', status=302, headers=headers)


@get('/(?P<profilename>[^/]+)/activity')
def activity(request, profilename):
    try:
        notes = t.users.get_events(profilename, limit=50)
        all_notes = notes.entries
    except typd.NotFound:
        raise itty.NotFound('No such profilename %r' % profilename)

    posts = (obj for obj in objs_for_notes(good_notes_for_notes(notes.entries)))

    return render(request, 'activity.html', {
        'profilename': profilename,
        'posts': posts,
    })


@get('/(?P<profilename>[^/]+)(?:/page/(?P<page>\d+))?')
def read(request, profilename, page):
    page = int(page) if page else 1
    offset = (page - 1) * 100
    try:
        notes = t.users.get_notifications(profilename, offset=1 + offset, limit=50)
        more_notes = t.users.get_notifications(profilename, offset=51 + offset, limit=50)
    except typd.NotFound:
        raise itty.NotFound('No such profilename %r' % profilename)

    noteiter = add_followers(profilename, notes.entries + more_notes.entries)
    followers = noteiter.next()
    posts = (obj for obj in objs_for_notes(good_notes_for_notes(noteiter), followers, profilename))

    return render(request, 'read.html', {
        'profilename': profilename,
        'posts': posts,
        'page': page,
    })


if __name__ == '__main__':
    try:
        execfile(join(dirname(__file__), 'settings.py'), settings)
    except IOError:
        pass
    configure()
    run_itty(host='0.0.0.0')
