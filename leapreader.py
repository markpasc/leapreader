from os.path import join, dirname
import random

from itty import get, run_itty
import itty
from jinja2 import Environment, FileSystemLoader
import typd


env = Environment(loader=FileSystemLoader(join(dirname(__file__), 'templates')))

t = typd.TypePad(endpoint='http://api.typepad.com/')

settings = {}


def render(templatename, data):
    t = env.get_template(templatename)
    return t.render(**data)


def random_rotation():
    while True:
        yield random.gauss(0, 3)


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


def objs_for_notes(notes):
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
            obj.why = obj.actions[0]
        yield obj


@get('/(?P<profilename>[^/]+)(?P<activity>/activity)?')
def read(request, profilename, activity):
    try:
        if activity:
            notes = t.users.get_events(profilename, limit=50)
            all_notes = notes.entries
        else:
            notes = t.users.get_notifications(profilename, offset=1, limit=50)
            more_notes = t.users.get_notifications(profilename, offset=51, limit=50)
            all_notes = notes.entries + more_notes.entries
    except typd.NotFound:
        raise itty.NotFound('No such profilename %r' % profilename)

    posts = (obj for obj in objs_for_notes(good_notes_for_notes(all_notes)))

    return render('read.html', {
        'profilename': profilename,
        'posts': posts,
        'rot': random_rotation(),
        'ganalytics_code': settings.get('ganalytics_code'),
    })


if __name__ == '__main__':
    try:
        execfile(join(dirname(__file__), 'settings.py'), settings)
    except IOError:
        pass
    run_itty(host='0.0.0.0')
