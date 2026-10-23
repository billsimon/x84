""" who's online web module for x/84. """

import web
import html
import json
from x84.bbs import timeago
from x84.bbs.ini import get_ini


def get_online():
    """
    Return users logged in, longest connected first.

    Each is a dict of ``handle``, ``activity``, and the seconds of
    ``idle`` time and connection ``duration``.  Sessions not yet logged
    in are not listed.
    """
    from x84.terminal import get_terminals
    online = []
    for _, tty in get_terminals():
        if not tty.info.get('handle'):
            continue
        online.append({
            'handle': tty.info['handle'],
            'activity': tty.info.get('activity', ''),
            'idle': int(tty.client.idle()),
            'duration': int(tty.client.duration()),
        })
    return sorted(online, reverse=True, key=lambda user: user['duration'])


class OnlineApi(object):

    """
    Who's online demonstration API endpoint

    Add ``?json`` for JSON, ``?text`` for plain text, or ``?embed`` for
    only the html list, suitable for embedding in another page.
    """

    def GET(self):
        """ Return users online """

        online = get_online()

        # output JSON instead?
        if 'json' in web.input(_method='get'):
            return json.dumps(online)

        board = get_ini('system', 'bbsname') or 'x/84'
        page_title = "Who's online at {board}".format(board=board)

        if 'text' in web.input(_method='get'):
            online_txt = ''
            for user in online:
                online_txt += ' --> {who}: {activity} ({idle} idle)\r\n'.format(
                    who=user['handle'], activity=user['activity'],
                    idle=timeago(user['idle']).strip())
            web.header('Content-Type', 'text/plain; charset=utf-8',
                       unique=True)
            return page_title + u'\r\n' + online_txt

        online_html = ''
        for user in online:
            online_html += ''.join((
                '<li class="list-group-item"><b>{who}:</b> {activity} ',
                '<small>({idle} idle)</small></li>')).format(
                who=html.escape(user['handle']),
                activity=html.escape(user['activity']),
                idle=timeago(user['idle']).strip())

        web.header('Content-Type', 'text/html; charset=utf-8', unique=True)
        if 'embed' in web.input(_method='get'):
            return """
                <ul class="list-group list-group-flush">
                    {online_html}
                </ul>
                """.format(online_html=online_html)

        output = """
            <!DOCTYPE html>
            <html lang="en-US">
            <head>
                <meta charset="utf-8" />
                <title>{page_title}</title>
            </head>
            <body>
                <h1>{page_title}</h1>
                <ul>
                    {online_html}
                </ul>
            </body>
            </html>
            """.format(page_title=html.escape(page_title),
                       online_html=online_html)
        return output


def web_module():
    """
    Setup the module and return a dict of its REST API.

    Called only once on server start.
    """

    return {
        'urls': ('/online/?', 'online'),
        'funcs': {
            'online': OnlineApi
        }
    }
