""" goodbye messages ('automsg' of logoff) web module for x/84. """

import web
import html
import json
import time
from x84.bbs import DBProxy, timeago
from x84.bbs.ini import get_ini


class AutomsgApi(object):

    """
    Goodbye messages demonstration API endpoint

    These are said by users as they log off.  Add ``?json`` for JSON,
    ``?text`` for plain text, or ``?embed`` for only the html list,
    suitable for embedding in another page.
    """

    def GET(self, num=10):
        """ Return last x goodbye messages """

        num = int(num)
        automsgs = sorted((int(k), v) for (k, v) in
                          DBProxy('automsg', use_session=False).items())
        last = automsgs[max(0, len(automsgs) - num):]

        # output JSON instead?
        if 'json' in web.input(_method='get'):
            return json.dumps(last)

        board = get_ini('system', 'bbsname') or 'x/84'
        page_title = 'Last {num} goodbye messages on {board}'.format(
            num=num, board=board)

        now = time.time()
        if 'text' in web.input(_method='get'):
            automsgs_txt = ''
            for _, (stamp, handle, msg) in last:
                automsgs_txt += ' --> {handle}: {msg} ({ago} ago)\r\n'.format(
                    handle=handle, msg=msg,
                    ago=timeago(now - stamp).strip())
            web.header('Content-Type', 'text/plain; charset=utf-8',
                       unique=True)
            return page_title + u'\r\n' + automsgs_txt

        automsgs_html = ''
        for _, (stamp, handle, msg) in last:
            automsgs_html += ''.join((
                '<li class="list-group-item"><b>{handle}:</b> {msg} ',
                '<small>({ago} ago)</small></li>')).format(
                handle=html.escape(handle),
                msg=html.escape(msg),
                ago=timeago(now - stamp).strip())

        web.header('Content-Type', 'text/html; charset=utf-8', unique=True)
        if 'embed' in web.input(_method='get'):
            return """
                <ul class="list-group list-group-flush">
                    {automsgs_html}
                </ul>
                """.format(automsgs_html=automsgs_html)

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
                    {automsgs_html}
                </ul>
            </body>
            </html>
            """.format(page_title=html.escape(page_title),
                       automsgs_html=automsgs_html)
        return output


def web_module():
    """
    Setup the module and return a dict of its REST API.

    Called only once on server start.
    """

    return {
        'urls': ('/automsg/([0-9]+)/?', 'automsg', '/automsg/?', 'automsg'),
        'funcs': {
            'automsg': AutomsgApi
        }
    }
