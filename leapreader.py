from os.path import join, dirname
import random

from itty import get, run_itty
import itty
from jinja2 import Environment, FileSystemLoader
import typd


env = Environment(loader=FileSystemLoader(join(dirname(__file__), 'templates')))

settings = {}

t = typd.TypePad(endpoint='http://api.typepad.com/')

cache = dict()


def configure():
    if 'memcached_servers' in settings:
        getdef = object()
        class Cache(object):
            def __init__(self, cache):
                self.cache = cache
            def __getitem__(self, key):
                return self.cache.get(key)
            def __setitem__(self, key, value):
                return self.cache.set(key, value)
            def get(self, key, default=getdef):
                ret = self.cache.get(key)
                if ret is not None:
                    return ret
                if default is not getdef:
                    return default
                return None

        import memcache
        global cache
        cache = Cache(memcache.Client(settings['memcached_servers'], debug=10))


def render(templatename, data):
    t = env.get_template(templatename)
    return t.render(rot=random_rotation(),
        ganalytics_code=settings.get('ganalytics_code'),
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
        return render('index.html', {
            'rot': random_rotation(),
            'ganalytics_code': settings.get('ganalytics_code'),
        })
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

        if note.verb == 'NewAsset':
            obj = note.object

            if obj is None:  # deleted asset
                continue
            if obj.permalink_url is None:  # no ancillary
                continue
            if obj.source is not None:  # no boomerang
                if obj.source.by_user:
                    continue
            if obj.container is not None and obj.container.url_id == '6p0120a5e990ac970c':
                continue

            if getattr(obj, 'reblog_of', None) is not None:
                note.original = obj
                note.verb = 'Reblog'
                obj = note.object = t.assets.get(obj.reblog_of.url_id)
            elif getattr(obj, 'root', None) is not None:
                note.original = obj
                note.verb = 'Comment'
                obj = note.object = t.assets.get(obj.root.url_id)

            okay_types = ['Post']
            if obj.container and obj.container.object_type == 'Group':
                okay_types.extend(['Photo', 'Audio', 'Video', 'Link'])
            if obj.object_type not in okay_types:
                continue

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

        if note.verb == 'NewAsset':
            objdata['new_asset'] = True
            objdata['when'] = note.published
        else:
            objdata['actions'].append(note)

    for objdata in sorted(interesting.values(), key=lambda d: d['when'], reverse=True):
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
            obj.why = obj.actions[0]
        yield obj


@get('/(?P<profilename>[^/]+)/activity')
def activity(request, profilename):
    try:
        notes = t.users.get_events(profilename, limit=50)
        all_notes = notes.entries
    except typd.NotFound:
        raise itty.NotFound('No such profilename %r' % profilename)

    posts = (obj for obj in objs_for_notes(good_notes_for_notes(notes.entries)))

    return render('activity.html', {
        'profilename': profilename,
        'posts': posts,
    })


@get('/(?P<profilename>[^/]+)')
def read(request, profilename):
    try:
        notes = t.users.get_notifications(profilename, offset=1, limit=50)
        more_notes = t.users.get_notifications(profilename, offset=51, limit=50)
    except typd.NotFound:
        raise itty.NotFound('No such profilename %r' % profilename)

    noteiter = add_followers(profilename, notes.entries + more_notes.entries)
    followers = noteiter.next()
    posts = (obj for obj in objs_for_notes(good_notes_for_notes(noteiter), followers, profilename))

    return render('read.html', {
        'profilename': profilename,
        'posts': posts,
    })


if __name__ == '__main__':
    try:
        execfile(join(dirname(__file__), 'settings.py'), settings)
    except IOError:
        pass
    configure()
    run_itty(host='0.0.0.0')
