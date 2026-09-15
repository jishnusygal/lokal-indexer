from datetime import timezone
from email.utils import format_datetime
from urllib.parse import urlencode
from xml.etree.ElementTree import Element, SubElement, tostring

NS = 'http://torznab.com/schemas/2015/feed'
from xml.etree.ElementTree import register_namespace
register_namespace('torznab', NS)

def xml(root):
    return tostring(root, encoding='utf-8', xml_declaration=True)

def error(code, description):
    return xml(Element('error', code=str(code), description=description))

def caps():
    root = Element('caps')
    SubElement(root, 'server', version='1.0', title='Lokal Indexer')
    SubElement(root, 'limits', max='100', default='100')
    searching = SubElement(root, 'searching')
    for name, params in [('search', 'q'), ('tv-search', 'q,season,ep'), ('movie-search', 'q')]:
        SubElement(searching, name, available='yes', supportedParams=params)
    categories = SubElement(root, 'categories')
    SubElement(categories, 'category', id='2000', name='Movies')
    SubElement(categories, 'category', id='5000', name='TV')
    return xml(root)

def feed(releases, total, offset, base_url, api_key):
    root = Element('rss', version='2.0')
    channel = SubElement(root, 'channel')
    for name, value in [('title', 'Lokal Indexer'), ('description', 'Locally cached media releases'), ('link', base_url)]:
        SubElement(channel, name).text = value
    SubElement(channel, f'{{{NS}}}response', offset=str(offset), total=str(total))
    for release in releases:
        item = SubElement(channel, 'item')
        url = (f'{base_url.rstrip("/")}/download/{release.id}?' + urlencode({'apikey': api_key})
               if release.torrent_file_path else release.magnet_uri)
        SubElement(item, 'title').text = release.title
        SubElement(item, 'guid', isPermaLink='false').text = release.id
        SubElement(item, 'link').text = url
        date = release.pub_date.replace(tzinfo=timezone.utc) if release.pub_date.tzinfo is None else release.pub_date.astimezone(timezone.utc)
        SubElement(item, 'pubDate').text = format_datetime(date, usegmt=True)
        SubElement(item, 'size').text = str(release.size)
        SubElement(item, 'category').text = '2000' if release.category == 'movie' else '5000'
        SubElement(item, 'enclosure', url=url or '', length=str(release.size), type='application/x-bittorrent')
        attrs = {'category': '2000' if release.category == 'movie' else '5000', 'size': release.size,
                 'infohash': release.id}
        for key, value in [('magneturl', release.magnet_uri), ('imdb', release.imdb_id), ('season', release.season), ('episode', release.episode)]:
            if value is not None:
                attrs[key] = value
        for key, value in attrs.items():
            SubElement(item, f'{{{NS}}}attr', name=key, value=str(value))
    return xml(root)
