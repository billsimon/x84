"""
Weather forecast script for x/84.

Weather data is provided by the free Open-Meteo API (https://open-meteo.com),
which requires no API key, under the terms of its CC BY 4.0 license.
"""
import itertools
import datetime
import textwrap
import requests
import warnings
import logging
import os


log = logging.getLogger(__name__)

weather_icons = os.path.join(os.path.abspath(
    os.path.dirname(__file__)), 'art', 'weather')
panel_width = 15
panel_height = 8
top_margin = 1
next_margin = 2
cf_key = u'!'

#: Open-Meteo geocoding api, for locations by city name or postal code.
GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'

#: Open-Meteo forecast api.
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'

#: seconds to wait for weather api responses.
REQUEST_TIMEOUT = 15

#: attribution required by the Open-Meteo license.
ATTRIBUTION = u'Weather data by Open-Meteo.com'

#: WMO weather interpretation codes, mapped to a description, and the
#: (AccuWeather-numbered) icon of 'art/weather/{icon}.ans' to display, by
#: day and by night.
WMO_CODES = {
    0: (u'Clear', 1, 33),
    1: (u'Mostly clear', 2, 34),
    2: (u'Partly cloudy', 3, 35),
    3: (u'Overcast', 7, 38),
    45: (u'Fog', 11, 11),
    48: (u'Freezing fog', 11, 11),
    51: (u'Light drizzle', 12, 39),
    53: (u'Drizzle', 12, 39),
    55: (u'Heavy drizzle', 18, 18),
    56: (u'Freezing drizzle', 26, 26),
    57: (u'Freezing drizzle', 26, 26),
    61: (u'Light rain', 12, 39),
    63: (u'Rain', 18, 18),
    65: (u'Heavy rain', 18, 18),
    66: (u'Freezing rain', 26, 26),
    67: (u'Freezing rain', 26, 26),
    71: (u'Light snow', 19, 43),
    73: (u'Snow', 22, 44),
    75: (u'Heavy snow', 22, 44),
    77: (u'Snow grains', 19, 43),
    80: (u'Rain showers', 14, 39),
    81: (u'Rain showers', 13, 40),
    82: (u'Heavy showers', 13, 40),
    85: (u'Snow showers', 21, 43),
    86: (u'Snow showers', 20, 43),
    95: (u'Thunderstorms', 15, 42),
    96: (u'T-storms, hail', 16, 42),
    99: (u'T-storms, hail', 16, 42),
}

#: compass directions, for wind direction in degrees.
COMPASS = (u'N', u'NNE', u'NE', u'ENE', u'E', u'ESE', u'SE', u'SSE',
           u'S', u'SSW', u'SW', u'WSW', u'W', u'WNW', u'NW', u'NNW')


def temp_conv(val, centigrade):
    """
    Convert temperature ``val`` (fahrenheit) to C or F, returning both the
    integer value and brief descriptor as tuple, fe. (33, u'F',).
    """
    try:
        val = float(val)
    except (TypeError, ValueError):
        return '', ''
    if not centigrade:
        return int(round(val)), u'F'
    return int(round((val - 32) * (5 / 9))), u'C'


def speed_conv(val, centigrade):
    """
    Convert windspeed ``val`` to MPH or KPH, returning both the integer
    value and brief descriptor as tuple, fe. (10, u'MPH',). We re-use
    the session boolean 'centigrade' as weather or not to use MPH or KPH,
    (centigrade is metric, otherwise imperial). This isn't 100% accurate,
    but close enough for our needs ..
    """
    # we simply use the 'centigrade' measurement as imperial vs. metric
    try:
        val = float(val)
    except (TypeError, ValueError):
        return '', ''
    if not centigrade:
        return int(round(val)), u'MPH'
    return int(round(val / 0.62137)), u'KPH'


def disp_msg(msg):
    """ Display unicode string ``msg`` in yellow. """
    from x84.bbs import getterminal, echo
    term = getterminal()
    msg = term.bold_yellow(msg)
    dotdot = term.yellow_reverse_bold(u'...')
    echo(u'\r\n\r\n{msg} {dotdot}'.format(msg=msg, dotdot=dotdot))


def disp_notfound():
    """ Display 'bad request -/- not found in red. """
    from x84.bbs import getterminal, echo
    term = getterminal()
    echo(u'\r\n\r\n{bad_req} {decorator} {not_found}'.format(
        bad_req=term.bold(u'bad request'),
        decorator=term.bold_red(u'-/-'),
        not_found=term.bold(u'not found.')))


def disp_found(num):
    """ Display 'N locations discovered' in yellow/white. """
    from x84.bbs import getterminal, echo
    term = getterminal()
    disp_n = term.bold_white(u'{}'.format(num))
    locations = term.yellow(u'Location{s} discovered'.format(
        s=u's' if num > 1 else u'',))
    dotdot = term.bold_black(u'...')
    echo(u'\r{disp_n} {locations} {dotdot}'.format(
        disp_n=disp_n, locations=locations, dotdot=dotdot))


def disp_search_help():
    """ Display searchbar usage. """
    from x84.bbs import getterminal, echo
    term = getterminal()

    enter = term.yellow(u'Enter a')
    city = term.bold_yellow(u'city')
    or_postal = term.yellow(u'or')
    postal = term.bold_yellow(u'postal code.')
    keyhelp = (u'{t.bold_yellow}({t.normal}'
               u'{t.underline_yellow}Escape{t.normal}'
               u'{t.bold_white}:{t.normal}'
               u'{t.yellow}exit{t.normal}'
               u'{t.bold_yellow}){t.normal}'.format(t=term))

    echo(u'\r\n\r\n' + term.normal)
    echo(u'\r\n'.join(
        term.wrap(u'{enter} {city} {or_postal} {postal} {keyhelp}'
                  .format(enter=enter, city=city, or_postal=or_postal,
                          postal=postal, keyhelp=keyhelp),
                  term.width)
    ))


def describe_wmo_code(code, is_day=True):
    """ Return tuple of (description, icon number) for WMO weather code. """
    try:
        description, day_icon, night_icon = WMO_CODES[int(code)]
    except (KeyError, TypeError, ValueError):
        return u'', 1
    return description, (day_icon if is_day else night_icon)


def fetch_weather(location):
    """
    Fetch and return today's weather and forecast for ``location``.

    :param dict location: location as returned by :func:`do_search`.
    :rtype: tuple
    :returns: tuple of ``(todays, forecast)``: a dictionary describing
        today's weather, and a list of dictionaries describing each
        day of the forecast; or ``(None, None)`` on failure.
    """
    disp_msg(u'fEtChiNG')
    try:
        resp = requests.get(FORECAST_URL, timeout=REQUEST_TIMEOUT, params={
            'latitude': location['latitude'],
            'longitude': location['longitude'],
            'current': ','.join((
                'temperature_2m', 'relative_humidity_2m',
                'apparent_temperature', 'is_day', 'weather_code',
                'wind_speed_10m', 'wind_direction_10m')),
            'daily': ','.join((
                'weather_code', 'temperature_2m_max', 'temperature_2m_min')),
            'timezone': 'auto',
            'forecast_days': 7,
            'temperature_unit': 'fahrenheit',
            'wind_speed_unit': 'mph',
        })
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError, KeyError) as err:
        log.warning('weather fetch failed: {0}'.format(err))
        disp_notfound()
        return None, None
    return (parse_todays_weather(data, location),
            parse_forecast(data))


def do_search(term, search):
    """ Given search string, return list of possible matching locations. """
    from x84.bbs import echo
    disp_msg(u'SEARChiNG')
    locations = list()
    try:
        resp = requests.get(GEOCODE_URL, timeout=REQUEST_TIMEOUT, params={
            # only the city of 'city, state' is searched for.
            'name': search.split(',')[0].strip(),
            'count': 20, 'language': 'en', 'format': 'json'})
        resp.raise_for_status()
        results = resp.json().get('results', [])
    except (requests.RequestException, ValueError) as err:
        echo(u'\r\n{0}\r\n\r\nPress any key'.format(err))
        term.inkey()
        return locations

    for result in results:
        locations.append({
            'id': result['id'],
            'city': result['name'],
            'state': result.get('admin1') or result.get('country', u''),
            'country': result.get('country_code', u''),
            'latitude': result['latitude'],
            'longitude': result['longitude'],
        })
    if 0 == len(locations):
        disp_notfound()
    else:
        disp_found(len(locations))
    return locations


def parse_todays_weather(data, location):
    """
    Parse and return dictionary describing today's weather
    from Open-Meteo forecast data.
    """
    current = data.get('current', {})
    description, icon = describe_wmo_code(current.get('weather_code'),
                                          bool(current.get('is_day', 1)))
    direction = current.get('wind_direction_10m')
    when = current.get('time', u'')
    return {
        'City': location.get('city', u''),
        'State': location.get('state', u''),
        'Time': when.partition('T')[2] or u'00:00',
        'Temperature': current.get('temperature_2m'),
        'RealFeel': current.get('apparent_temperature'),
        'WindSpeed': current.get('wind_speed_10m'),
        'WindDirection': (COMPASS[int((direction % 360) / 22.5 + 0.5) % 16]
                          if direction is not None else u''),
        'Humidity': u'{0}%'.format(current.get('relative_humidity_2m', u'')),
        'WeatherText': description,
        'WeatherIcon': icon,
    }


def parse_forecast(data):
    """
    Parse and return list of dictionaries describing weather forecast
    from Open-Meteo forecast data.
    """
    daily = data.get('daily', {})
    forecast = []
    for idx, day in enumerate(daily.get('time', [])):
        description, icon = describe_wmo_code(daily['weather_code'][idx])
        forecast.append({
            'DayCode': datetime.date.fromisoformat(day).strftime('%A'),
            'WeatherIcon': icon,
            'High_Temperature': daily['temperature_2m_max'][idx],
            'Low_Temperature': daily['temperature_2m_min'][idx],
            'TXT_Short': description,
        })
    return forecast


def get_centigrade():
    """ Blocking prompt for setting C/F preference. """
    from x84.bbs import getterminal, getsession, echo
    term = getterminal()
    session = getsession()
    if bool(session.user.handle == 'anonymous'):
        # anonymous cannot set a preference.
        return

    echo(u''.join((
        u'\r\n\r\n',
        term.yellow(u'Celcius'),
        term.bold_yellow(u'('),
        term.bold_yellow_reverse(u'C'),
        term.bold_yellow(u')'),
        u' or ',
        term.yellow(u'Fahrenheit'),
        term.bold_yellow(u'('),
        term.bold_yellow_reverse(u'F'),
        term.bold_yellow(u')'),
        u'? ')))

    while True:
        inp = term.inkey()
        if inp in (u'c', u'C'):
            session.user['centigrade'] = True
            break
        elif inp in (u'f', u'F'):
            session.user['centigrade'] = False
            break
        elif inp in (u'q', u'Q') or inp.code == term.KEY_ESCAPE:
            break


def chk_centigrade():
    """
    Provide hint for setting C/F preference (! key)
    """
    from x84.bbs import getterminal, getsession, echo
    session, term = getsession(), getterminal()
    echo(u'\r\n\r\n')
    echo(u'USiNG ')
    if session.user.get('centigrade', None):
        echo(term.yellow(u'Celcius'))
    else:
        echo(term.yellow(u'Fahrenheit'))
    echo(term.bold_black('...'))


def chk_save_location(location):
    """
    Prompt user to save location for quick re-use
    """
    from x84.bbs import getterminal, getsession, echo
    session, term = getsession(), getterminal()
    if location == session.user.get('location', dict()):
        # location already saved
        return False
    if session.user.handle == 'anonymous':
        # anonymous cannot save preferences
        return False

    # prompt to store (unsaved/changed) location
    echo(u'\r\n\r\n')
    echo(term.yellow(u'Save Location'))
    echo(term.bold_yellow(u' ('))
    echo(term.bold_black(u'private'))
    echo(term.bold_yellow(u') '))
    echo(term.yellow(u'? '))
    echo(term.bold_yellow(u'['))
    echo(term.underline_yellow(u'yn'))
    echo(term.bold_yellow(u']'))
    echo(u': ')
    while True:
        inp = term.inkey()
        if inp.code == term.KEY_ESCAPE or inp.lower() in (u'n', 'q'):
            break
        elif inp.code == term.KEY_ENTER or inp.lower() in (u'y', u' '):
            session.user['location'] = location
            break


def location_name(location):
    """ Return name of location as 'city, state'. """
    return u', '.join(_part for _part in (
        location.get('city', u''), location.get('state', u'')) if _part)


def get_zipsearch(zipcode=u''):
    """
    Prompt user for zipcode or international city.
    """
    from x84.bbs import getterminal, LineEditor, echo
    term = getterminal()
    echo(u''.join((u'\r\n\r\n',
                   term.bold_yellow(u'  -'),
                   term.reverse_yellow(u':'),
                   u' ')))
    return LineEditor(width=min(30, term.width - 5), content=zipcode).read()


def chose_location_lightbar(locations):
    """
    Lightbar pager for chosing a location.
    """
    from x84.bbs import getterminal, echo, Lightbar
    term = getterminal()
    lookup = dict([(loc['id'], loc) for loc in locations])
    fullheight = min(term.height - 8, len(locations) + 2)
    fullwidth = min(75, int(term.width * .8))
    # shrink window to minimum width
    maxwidth = max([len(location_name(val)) for val in lookup.values()]) + 2
    if maxwidth < fullwidth:
        fullwidth = maxwidth
    echo(u'\r\n' * fullheight)
    lightbar = Lightbar(height=fullheight,
                        width=fullwidth,
                        yloc=term.height - fullheight,
                        xloc=int((term.width / 2) - (fullwidth / 2)))
    lightbar.update([(loc['id'], location_name(loc)) for loc in locations])
    lightbar.colors['border'] = term.yellow
    echo(lightbar.border())
    echo(lightbar.title(u''.join((
        term.yellow(u'-'), term.bold_white(u'[ '),
        term.bold_yellow('CitY'),
        term.bold_white(u', '),
        term.bold_yellow('StAtE'),
        term.bold_white(u' ]'), term.yellow(u'-'),))))
    echo(lightbar.footer(u''.join((
        term.yellow(u'-'), term.bold_black(u'( '),
        term.yellow_underline('Escape'), u':',
        term.yellow('EXit'),
        term.bold_black(u' )'), term.yellow(u'-'),))))
    lightbar.colors['highlight'] = term.yellow_reverse
    choice = lightbar.read()
    echo(lightbar.erase())
    return lookup.get(choice)


def chose_location(locations):
    """
    Prompt user to chose a location.
    """
    from x84.bbs import getterminal, echo
    term = getterminal()
    assert len(locations) > 0, locations
    echo(u'\r\n\r\n {chose_a} {city}: '
         .format(chose_a=term.yellow(u'chose a'),
                 city=term.bold_yellow('city')))
    return chose_location_lightbar(locations)


def location_prompt(location, msg='WEAthER'):
    """
    Prompt user to display weather or forecast.
    """
    from x84.bbs import getterminal, echo
    term = getterminal()
    echo(u''.join((u'\r\n\r\n',
                   term.yellow(u'Display %s for ' % (msg,)),
                   term.bold(location_name(location)),
                   term.yellow(' ? '),
                   term.bold_yellow(u'['),
                   term.underline_yellow(u'yn'),
                   term.bold_yellow(u']'),
                   u': '),))
    while True:
        inp = term.inkey()
        if inp.lower() in (u'n', 'q', '\x1b'):
            return False
        elif inp.lower() in (u'y', u' ', u'\r', u'\n'):
            return True


def get_icon(weather):
    # attribute 'WeatherIcon' is mapped to one of the {}.ans files
    icon = int(weather.get('WeatherIcon', '1'))
    artfile = os.path.join(weather_icons, '{}.ans'.format(icon))
    if not os.path.exists(artfile):
        warnings.warn('{} not found'.format(artfile))
        return [u'[ .{:>2}. ]'.format(icon)]
    with open(artfile, 'rb') as fin:
        return fin.read().decode('cp437_art').splitlines()


def display_panel(weather, column, centigrade):
    from x84.bbs import getterminal, echo
    term = getterminal()

    # display day of week,
    day_txt = term.bold(weather.get('DayCode', u'').center(panel_width))
    echo(term.move(top_margin, column))
    echo(day_txt)

    # display WeatherIcon ansi art,
    for row_idx, art_row in enumerate(get_icon(weather)):
        echo(term.move(row_idx + top_margin + 1, column))
        echo(art_row)
    echo(term.normal)

    degree = b'\xf8'.decode('cp437_art')
    # display days' high,
    echo(term.move(panel_height + top_margin + 1, column))
    high = weather.get('High_Temperature', None)
    high, conv = temp_conv(high, centigrade)
    echo(u'High: {high:>2}{degree}{conv}'.format(
        high=high, degree=degree, conv=conv).rjust(panel_width - 3))

    # display days' low,
    echo(term.move(panel_height + top_margin + 2, column))
    low = weather.get('Low_Temperature', None)
    low, conv = temp_conv(low, centigrade)
    echo(u'Low: {low:>2}{degree}{conv}'.format(
        low=low, degree=degree, conv=conv).rjust(panel_width - 3))

    # display short txt,
    weather_txt = str(weather.get('TXT_Short', ''))
    txt_wrapped = textwrap.wrap(weather_txt, (panel_width - 2))
    row_loc = panel_height + top_margin + 3

    for row_idx, txt_row in enumerate(txt_wrapped):
        row_loc = panel_height + top_margin + row_idx + 4
        echo(term.move(row_loc, column + 1))
        echo(txt_row.center(panel_width - 2))
    return row_loc


def display_weather(todays, forecast, centigrade):
    """
    Display weather as vertical panels.

    Thanks to xzip, we now have a sortof tv-weather channel art :-)
    """
    from x84.bbs import getterminal, echo, syncterm_setfont
    term = getterminal()
    # set syncterm font to cp437
    if term.kind.startswith('ansi'):
        echo(syncterm_setfont('cp437'))

    echo(term.height * u'\r\n')
    echo(term.move(0, 0))
    city = term.bold(todays.get('City', u''))
    state = todays.get('State', u'')
    if state:
        state = u', {}'.format(term.bold(state))
    dotdot = term.bold_black('...')
    echo(u'At {city}{state} {dotdot}'.format(
        city=city, state=state, dotdot=dotdot))

    bottom = 3
    if forecast:
        end = (term.width - panel_width)
        step = panel_width
        for idx, column in enumerate(range(0, end, step)):
            try:
                day = forecast[idx]
            except IndexError:
                break
            bottom = max(display_panel(day, column, centigrade), bottom)

    timenow = datetime.datetime.strptime(
        todays.get('Time', '00:00'), '%H:%M').strftime('%I:%M%p')
    temp, deg_conv = temp_conv(todays.get('Temperature', ''), centigrade)
    real_temp, deg_conv = temp_conv(todays.get('RealFeel', ''), centigrade)
    speed, spd_conv = speed_conv(todays.get('WindSpeed', ''), centigrade)
    degree = b'\xf8'.decode('cp437_art')

    current_0 = u'Current conditions at {timenow}'.format(timenow=timenow)
    current_1 = u'{0}'.format(todays.get('WeatherText', ''))
    current_2 = u'Temperature is {temp}{degree}{deg_conv}'.format(
        temp=temp, degree=degree, deg_conv=deg_conv)
    current_3 = u'' if real_temp == temp else (
        u'(feels like {real_temp}{degree}{deg_conv})'.format(
            real_temp=real_temp, degree=degree, deg_conv=deg_conv))
    current_4 = u'Winds {speed}{spd_conv} {wind}'.format(
        speed=speed, spd_conv=spd_conv,
        wind=todays.get('WindDirection', ''))
    current_5 = u'Humidity of {0}'.format(todays.get('Humidity', ''))

    temperature = u' '.join(_txt for _txt in (current_2, current_3) if _txt)
    wrapped = textwrap.wrap(
        u'{0}: {1}. {2}, {3}, {4}.'.format(
            current_0, current_1, temperature, current_4, current_5),
        min(term.width - panel_width - 2, 40))
    row_num = 0

    art = get_icon(todays)
    joined_art_conditions = list(itertools.zip_longest(wrapped, art))
    last_line = lambda row_num: row_num == len(joined_art_conditions) - 1
    for row_num, (row_txt, art_txt) in enumerate(joined_art_conditions):
        echo(term.move(bottom + next_margin + row_num, 1))
        echo(art_txt)
        if not row_txt and not last_line(row_num):
            echo(u'\r\n')
        elif row_txt:
            echo(term.move(bottom + next_margin + row_num, panel_width + 5))
            echo(term.normal)
            echo(row_txt)


def main():
    """ Main routine. """
    from x84.bbs import getsession, getterminal, echo
    session, term = getsession(), getterminal()
    session.activity = 'Weather'

    while True:
        echo(u'\r\n\r\n')
        location = session.user.get('location', dict())
        disp_search_help()
        search = get_zipsearch(location_name(location))
        if search is None or 0 == len(search):
            # exit (no selection)
            return

        locations = do_search(term, search)
        if 0 == len(locations):
            continue

        location = (locations.pop() if 1 == len(locations)
                    else chose_location(locations))
        if location is None:
            # canceled
            continue

        todays, forecast = fetch_weather(location)
        if todays is None:
            # exit (weather not found)
            return

        if session.user.get('centigrade', None) is None:
            # request C/F preference,
            get_centigrade()
        else:
            # offer C/F preference change
            chk_centigrade()

        while True:
            centigrade = session.user.get('centigrade', False)

            display_weather(todays, forecast, centigrade)
            txt_chg_deg = (', [{0}]: change degrees'.format(cf_key)
                           if session.user.handle != 'anonymous' else u'')
            echo(u''.join((term.normal, u'\r\n\r\n',
                           term.move_x(5),
                           term.bold_black(ATTRIBUTION),
                           u'\r\n', term.move_x(5),
                           u'-- press return' + txt_chg_deg + ' --')))

            while True:
                # allow re-displaying weather between C/F, even at EOT prompt
                inp = term.inkey()
                if inp.lower() == cf_key:
                    get_centigrade()
                    break
                elif inp.code == term.KEY_ENTER or inp in (u'\r', u'\n'):
                    chk_save_location(location)
                    return
