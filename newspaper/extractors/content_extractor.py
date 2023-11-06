import logging
import re
from collections import defaultdict
from datetime import datetime
import json
from typing import List, Optional, OrderedDict

from dateutil.parser import parse as date_parser
import lxml
from tldextract import tldextract
from urllib.parse import urlparse, urlunparse

from newspaper import urls
from newspaper.configuration import Configuration
from newspaper.extractors.articlebody_extractor import ArticleBodyExtractor
from newspaper.extractors.image_extractor import ImageExtractor
from newspaper.extractors.videos_extractor import VideoExtractor
from newspaper.extractors.defines import (
    AUTHOR_ATTRS,
    AUTHOR_STOP_WORDS,
    AUTHOR_VALS,
    MOTLEY_REPLACEMENT,
    TITLE_REPLACEMENTS,
    PIPE_SPLITTER,
    DASH_SPLITTER,
    UNDERSCORE_SPLITTER,
    SLASH_SPLITTER,
    ARROWS_SPLITTER,
    RE_LANG,
    PUBLISH_DATE_TAGS,
    A_REL_TAG_SELECTOR,
    A_HREF_TAG_SELECTOR,
    url_stopwords,
)

log = logging.getLogger(__name__)


class ContentExtractor(object):
    def __init__(self, config: Configuration):
        self.config = config
        self.parser = self.config.get_parser()
        self.language = config.language
        self.stopwords_class = config.stopwords_class
        self.atricle_body_extractor = ArticleBodyExtractor(config)
        self.image_extractor = ImageExtractor(config)
        self.video_extractor = VideoExtractor(config)

    def update_language(self, meta_lang):
        """Required to be called before the extraction process in some
        cases because the stopwords_class has to set in case the lang
        is not latin based
        """
        if meta_lang:
            self.language = meta_lang
            self.stopwords_class = self.config.get_stopwords_class(meta_lang)
            self.atricle_body_extractor.stopwords_class = self.stopwords_class
            self.atricle_body_extractor.language = self.language

    def get_authors(self, doc):
        """Fetch the authors of the article, return as a list
        Only works for english articles
        """
        _digits = re.compile(r"\d")
        author_stopwords = [re.escape(x) for x in AUTHOR_STOP_WORDS]
        author_stopwords = re.compile(
            r"\b(" + "|".join(author_stopwords) + r")\b", flags=re.IGNORECASE
        )

        def contains_digits(d):
            return bool(_digits.search(d))

        def uniqify_list(lst: List[str]) -> List[str]:
            """Remove duplicates from provided list but maintain original order.
            Ignores trailing spaces and case.

            Args:
                lst (List[str]): Input list of strings, with potential duplicates

            Returns:
                List[str]: Output list of strings, with duplicates removed
            """
            seen = OrderedDict()
            for item in lst:
                seen[item.lower().strip()] = item.strip()
            return [seen[item] for item in seen.keys() if item]

        def parse_byline(search_str):
            """
            Takes a candidate line of html or text and
            extracts out the name(s) in list form:
            >>> parse_byline('<div>By: <strong>Lucas Ou-Yang</strong>,
                    <strong>Alex Smith</strong></div>')
            ['Lucas Ou-Yang', 'Alex Smith']
            """
            # Remove HTML boilerplate
            search_str = re.sub("<[^<]+?>", "", search_str)
            search_str = re.sub("[\n\t\r\xa0]", " ", search_str)

            # Remove original By statement
            search_str = re.sub(r"[bB][yY][\:\s]|[fF]rom[\:\s]", "", search_str)

            search_str = search_str.strip()

            # Chunk the line by non alphanumeric
            # tokens (few name exceptions)
            # >>> re.split("[^\w\'\-\.]",
            #           "Tyler G. Jones, Lucas Ou, Dean O'Brian and Ronald")
            # ['Tyler', 'G.', 'Jones', '', 'Lucas', 'Ou', '',
            #           'Dean', "O'Brian", 'and', 'Ronald']
            name_tokens = re.split(r"[^\w'\-\.]", search_str)
            name_tokens = [s.strip() for s in name_tokens]

            _authors = []
            # List of first, last name tokens
            curname = []
            delimiters = ["and", ",", ""]

            for token in name_tokens:
                if token in delimiters:
                    if len(curname) > 0:
                        _authors.append(" ".join(curname))
                        curname = []

                elif not contains_digits(token):
                    curname.append(token)

            # One last check at end
            valid_name = len(curname) >= 2
            if valid_name:
                _authors.append(" ".join(curname))

            return _authors

        # Try 1: Search popular author tags for authors

        matches = []
        authors = []

        for attr in AUTHOR_ATTRS:
            for val in AUTHOR_VALS:
                # found = doc.xpath('//*[@%s="%s"]' % (attr, val))
                found = self.parser.getElementsByTag(doc, attr=attr, value=val)
                matches.extend(found)

        for match in matches:
            content = ""
            if match.tag == "meta":
                mm = match.xpath("@content")
                if len(mm) > 0:
                    content = mm[0]
            else:
                content = list(match.itertext())
                content = " ".join(content)
            if len(content) > 0:
                authors.extend(parse_byline(content))

        # Clean up authors of stopwords such as Reporter, Senior Reporter
        authors = [re.sub(author_stopwords, "", x) for x in authors]

        return uniqify_list(authors)

        # TODO Method 2: Search raw html for a by-line
        # match = re.search('By[\: ].*\\n|From[\: ].*\\n', html)
        # try:
        #    # Don't let zone be too long
        #    line = match.group(0)[:100]
        #    authors = parse_byline(line)
        # except:
        #    return [] # Failed to find anything
        # return authors

    def get_publishing_date(self, url, doc):
        """3 strategies for publishing date extraction. The strategies
        are descending in accuracy and the next strategy is only
        attempted if a preferred one fails.

        1. Pubdate from URL
        2. Pubdate from metadata
        3. Raw regex searches in the HTML + added heuristics
        """

        def parse_date_str(date_str):
            if date_str:
                try:
                    return date_parser(date_str)
                except (ValueError, OverflowError, AttributeError, TypeError):
                    # near all parse failures are due to URL dates without a day
                    # specifier, e.g. /2014/04/
                    return None

        date_matches = []
        date_match = re.search(urls.STRICT_DATE_REGEX, url)
        if date_match:
            date_str = date_match.group(0)
            datetime_obj = parse_date_str(date_str)
            if datetime_obj:
                date_matches.append((datetime_obj, 10))  # date and matchscore

        # yoast seo structured data
        yoast_script_tag = self.parser.getElementsByTag(
            doc, tag="script", attr="type", value="application/ld+json"
        )
        # TODO: get author names from Json-LD
        if yoast_script_tag:
            for script_tag in yoast_script_tag:
                if "yoast-schema-graph" in script_tag.attrib.get("class", ""):
                    try:
                        schema_json = json.loads(script_tag.text)
                    except Exception:
                        continue

                    g = schema_json.get("@graph", [])
                    for item in g:
                        date_str = item.get("datePublished")
                        datetime_obj = parse_date_str(date_str)
                        if datetime_obj:
                            date_matches.append((datetime_obj, 10))
                else:
                    # Some other type of Json-LD
                    m = re.search(
                        "[\"']datePublished[\"']\s?:\s?[\"']([^\"']+)[\"']",
                        script_tag.text,
                    )
                    if m:
                        date_str = m.group(1)
                        datetime_obj = parse_date_str(date_str)
                        if datetime_obj:
                            date_matches.append((datetime_obj, 9))

        for known_meta_tag in PUBLISH_DATE_TAGS:
            meta_tags = self.parser.getElementsByTag(
                doc, attr=known_meta_tag["attribute"], value=known_meta_tag["value"]
            )
            for meta_tag in meta_tags:
                date_str = self.parser.getAttribute(meta_tag, known_meta_tag["content"])
                datetime_obj = parse_date_str(date_str)
                if datetime_obj:
                    score = 6
                    if meta_tag.attrib.get("name") == known_meta_tag["value"]:
                        score += 2
                    days_diff = (datetime.now().date() - datetime_obj.date()).days
                    if days_diff < 0:  # articles from the future
                        score -= 2
                    elif days_diff > 25 * 365:  # very old articles
                        score -= 1
                    date_matches.append((datetime_obj, score))

        date_matches.sort(key=lambda x: x[1], reverse=True)
        return date_matches[0][0] if date_matches else None

    def get_title(self, doc):
        """Fetch the article title and analyze it

        Assumptions:
        - title tag is the most reliable (inherited from Goose)
        - h1, if properly detected, is the best (visible to users)
        - og:title and h1 can help improve the title extraction
        - python == is too strict, often we need to compare filtered
          versions, i.e. lowercase and ignoring special chars

        Explicit rules:
        1. title == h1, no need to split
        2. h1 similar to og:title, use h1
        3. title contains h1, title contains og:title, len(h1) > len(og:title), use h1
        4. title starts with og:title, use og:title
        5. use title, after splitting
        """
        title = ""
        title_element = self.parser.getElementsByTag(doc, tag="title")
        # no title found
        if title_element is None or len(title_element) == 0:
            return title

        # title elem found
        title_text = self.parser.getText(title_element[0])
        used_delimeter = False

        # title from h1
        # - extract the longest text from all h1 elements
        # - too short texts (fewer than 2 words) are discarded
        # - clean double spaces
        title_text_h1 = ""
        title_element_h1_list = self.parser.getElementsByTag(doc, tag="h1") or []
        title_text_h1_list = [self.parser.getText(tag) for tag in title_element_h1_list]
        if title_text_h1_list:
            # sort by len and set the longest
            title_text_h1_list.sort(key=len, reverse=True)
            title_text_h1 = title_text_h1_list[0]
            # discard too short texts
            if len(title_text_h1.split(" ")) <= 2:
                title_text_h1 = ""
            # clean double spaces
            title_text_h1 = " ".join([x for x in title_text_h1.split() if x])

        # title from og:title
        title_text_fb = (
            self.get_meta_content(doc, 'meta[property="og:title"]')
            or self.get_meta_content(doc, 'meta[name="og:title"]')
            or ""
        )

        # create filtered versions of title_text, title_text_h1, title_text_fb
        # for finer comparison
        filter_regex = re.compile(r"[^\u4e00-\u9fa5a-zA-Z0-9\ ]")
        filter_title_text = filter_regex.sub("", title_text).lower()
        filter_title_text_h1 = filter_regex.sub("", title_text_h1).lower()
        filter_title_text_fb = filter_regex.sub("", title_text_fb).lower()

        # check for better alternatives for title_text and possibly skip splitting
        if title_text_h1 == title_text:
            used_delimeter = True
        elif filter_title_text_h1 and filter_title_text_h1 == filter_title_text_fb:
            title_text = title_text_h1
            used_delimeter = True
        elif (
            filter_title_text_h1
            and filter_title_text_h1 in filter_title_text
            and filter_title_text_fb
            and filter_title_text_fb in filter_title_text
            and len(title_text_h1) > len(title_text_fb)
        ):
            title_text = title_text_h1
            used_delimeter = True
        elif (
            filter_title_text_fb
            and filter_title_text_fb != filter_title_text
            and filter_title_text.startswith(filter_title_text_fb)
        ):
            title_text = title_text_fb
            used_delimeter = True

        # split title with |
        if not used_delimeter and "|" in title_text:
            title_text = self.split_title(title_text, PIPE_SPLITTER, title_text_h1)
            used_delimeter = True

        # split title with -
        if not used_delimeter and "-" in title_text:
            title_text = self.split_title(title_text, DASH_SPLITTER, title_text_h1)
            used_delimeter = True

        # split title with _
        if not used_delimeter and "_" in title_text:
            title_text = self.split_title(
                title_text, UNDERSCORE_SPLITTER, title_text_h1
            )
            used_delimeter = True

        # split title with /
        if not used_delimeter and "/" in title_text:
            title_text = self.split_title(title_text, SLASH_SPLITTER, title_text_h1)
            used_delimeter = True

        # split title with »
        if not used_delimeter and " » " in title_text:
            title_text = self.split_title(title_text, ARROWS_SPLITTER, title_text_h1)
            used_delimeter = True

        title = MOTLEY_REPLACEMENT.replaceAll(title_text)

        # in some cases the final title is quite similar to title_text_h1
        # (either it differs for case, for special chars, or it's truncated)
        # in these cases, we prefer the title_text_h1
        filter_title = filter_regex.sub("", title).lower()
        if filter_title_text_h1 == filter_title:
            title = title_text_h1

        return title

    def split_title(self, title, splitter, hint=None):
        """Split the title to best part possible"""
        large_text_length = 0
        large_text_index = 0
        title_pieces = splitter.split(title)

        if hint:
            filter_regex = re.compile(r"[^a-zA-Z0-9\ ]")
            hint = filter_regex.sub("", hint).lower()

        # find the largest title piece
        for i, title_piece in enumerate(title_pieces):
            current = title_piece.strip()
            if hint and hint in filter_regex.sub("", current).lower():
                large_text_index = i
                break
            if len(current) > large_text_length:
                large_text_length = len(current)
                large_text_index = i

        # replace content
        title = title_pieces[large_text_index]
        return TITLE_REPLACEMENTS.replaceAll(title).strip()

    def get_feed_urls(self, source_url, categories):
        """Takes a source url and a list of category objects and returns
        a list of feed urls
        """
        total_feed_urls = []
        for category in categories:
            kwargs = {"attr": "type", "value": "application/rss+xml"}
            feed_elements = self.parser.getElementsByTag(category.doc, **kwargs)
            feed_urls = [e.get("href") for e in feed_elements if e.get("href")]
            total_feed_urls.extend(feed_urls)

        total_feed_urls = total_feed_urls[:50]
        total_feed_urls = [urls.prepare_url(f, source_url) for f in total_feed_urls]
        total_feed_urls = list(set(total_feed_urls))
        return total_feed_urls

    def get_meta_lang(self, doc):
        """Extract content language from meta"""
        # we have a lang attribute in html
        attr = self.parser.getAttribute(doc, attr="lang")
        if attr is None:
            # look up for a Content-Language in meta
            items = [
                {"tag": "meta", "attr": "http-equiv", "value": "content-language"},
                {"tag": "meta", "attr": "name", "value": "lang"},
            ]
            for item in items:
                meta = self.parser.getElementsByTag(doc, **item)
                if meta:
                    attr = self.parser.getAttribute(meta[0], attr="content")
                    break
        if attr:
            value = attr[:2]
            if re.search(RE_LANG, value):
                return value.lower()

        return None

    def get_meta_content(self, doc, metaname):
        """Extract a given meta content form document.
        Example metaNames:
            "meta[name=description]"
            "meta[name=keywords]"
            "meta[property=og:type]"
        """
        meta = self.parser.css_select(doc, metaname)
        content = None
        if meta is not None and len(meta) > 0:
            content = self.parser.getAttribute(meta[0], "content")
        if content:
            return content.strip()
        return ""

    def parse_images(
        self, article_url: str, doc: lxml.html.Element, top_node: lxml.html.Element
    ):
        """Parse images in an article"""
        self.image_extractor.parse(doc, top_node, article_url)

    def get_meta_type(self, doc):
        """Returns meta type of article, open graph protocol"""
        return self.get_meta_content(doc, 'meta[property="og:type"]')

    def get_meta_site_name(self, doc):
        """Returns site name of article, open graph protocol"""
        return self.get_meta_content(doc, 'meta[property="og:site_name"]')

    def get_meta_description(self, doc):
        """If the article has meta description set in the source, use that"""
        return self.get_meta_content(doc, "meta[name=description]")

    def get_meta_keywords(self, doc):
        """If the article has meta keywords set in the source, use that"""
        return self.get_meta_content(doc, "meta[name=keywords]")

    def get_meta_data(self, doc):
        data = defaultdict(dict)
        properties = self.parser.css_select(doc, "meta")
        for prop in properties:
            key = prop.attrib.get("property") or prop.attrib.get("name")
            value = prop.attrib.get("content") or prop.attrib.get("value")

            if not key or not value:
                continue

            key, value = key.strip(), value.strip()
            if value.isdigit():
                value = int(value)

            if ":" not in key:
                data[key] = value
                continue

            key = key.split(":")
            key_head = key.pop(0)
            ref = data[key_head]

            if isinstance(ref, str) or isinstance(ref, int):
                data[key_head] = {key_head: ref}
                ref = data[key_head]

            for idx, part in enumerate(key):
                if idx == len(key) - 1:
                    ref[part] = value
                    break
                if not ref.get(part):
                    ref[part] = dict()
                elif isinstance(ref.get(part), str) or isinstance(ref.get(part), int):
                    # Not clear what to do in this scenario,
                    # it's not always a URL, but an ID of some sort
                    ref[part] = {"identifier": ref[part]}
                ref = ref[part]
        return data

    def get_canonical_link(self, article_url, doc):
        """
        Return the article's canonical URL

        Gets the first available value of:
        1. The rel=canonical tag
        2. The og:url tag
        """
        links = self.parser.getElementsByTag(
            doc, tag="link", attr="rel", value="canonical"
        )

        canonical = self.parser.getAttribute(links[0], "href") if links else ""
        og_url = self.get_meta_content(doc, 'meta[property="og:url"]')
        meta_url = canonical or og_url or ""
        if meta_url:
            meta_url = meta_url.strip()
            parsed_meta_url = urlparse(meta_url)
            if not parsed_meta_url.hostname:
                # MIGHT not have a hostname in meta_url
                # parsed_url.path might be 'example.com/article.html' where
                # clearly example.com is the hostname
                parsed_article_url = urlparse(article_url)
                strip_hostname_in_meta_path = re.match(
                    ".*{}(?=/)/(.*)".format(parsed_article_url.hostname),
                    parsed_meta_url.path,
                )
                try:
                    true_path = strip_hostname_in_meta_path.group(1)
                except AttributeError:
                    true_path = parsed_meta_url.path

                # true_path may contain querystrings and fragments
                meta_url = urlunparse(
                    (
                        parsed_article_url.scheme,
                        parsed_article_url.hostname,
                        true_path,
                        "",
                        "",
                        "",
                    )
                )

        return meta_url

    def _get_urls(self, doc, titles):
        """Return a list of urls or a list of (url, title_text) tuples
        if specified.
        """
        if doc is None:
            return []

        a_kwargs = {"tag": "a"}
        a_tags = self.parser.getElementsByTag(doc, **a_kwargs)

        # TODO: this should be refactored! We should have a separate
        # method which siphones the titles our of a list of <a> tags.
        if titles:
            return [(a.get("href"), a.text) for a in a_tags if a.get("href")]
        return [a.get("href") for a in a_tags if a.get("href")]

    def get_urls(self, doc_or_html, titles=False, regex=False):
        """`doc_or_html`s html page or doc and returns list of urls, the regex
        flag indicates we don't parse via lxml and just search the html.
        """
        if doc_or_html is None:
            log.critical("Must extract urls from either html, text or doc!")
            return []
        # If we are extracting from raw text
        if regex:
            doc_or_html = re.sub("<[^<]+?>", " ", str(doc_or_html))
            doc_or_html = re.findall(
                r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|"
                "(?:%[0-9a-fA-F][0-9a-fA-F]))+",
                doc_or_html,
            )
            doc_or_html = [i.strip() for i in doc_or_html]
            return doc_or_html or []
        # If the doc_or_html is html, parse it into a root
        if isinstance(doc_or_html, str):
            doc = self.parser.fromstring(doc_or_html)
        else:
            doc = doc_or_html
        return self._get_urls(doc, titles)

    def get_category_urls(self, source_url, doc):
        """Inputs source lxml root and source url, extracts domain and
        finds all of the top level urls, we are assuming that these are
        the category urls.
        cnn.com --> [cnn.com/latest, world.cnn.com, cnn.com/asia]
        """
        page_urls = self.get_urls(doc)
        valid_categories = []
        for p_url in page_urls:
            scheme = urls.get_scheme(p_url, allow_fragments=False)
            domain = urls.get_domain(p_url, allow_fragments=False)
            path = urls.get_path(p_url, allow_fragments=False)

            if not domain and not path:
                if self.config.verbose:
                    print("elim category url %s for no domain and path" % p_url)
                continue
            if path and path.startswith("#"):
                if self.config.verbose:
                    print("elim category url %s path starts with #" % p_url)
                continue
            if scheme and (scheme != "http" and scheme != "https"):
                if self.config.verbose:
                    print(
                        "elim category url %s for bad scheme, not http nor https"
                        % p_url
                    )
                continue

            if domain:
                child_tld = tldextract.extract(p_url)
                domain_tld = tldextract.extract(source_url)
                child_subdomain_parts = child_tld.subdomain.split(".")
                subdomain_contains = False
                for part in child_subdomain_parts:
                    if part == domain_tld.domain:
                        if self.config.verbose:
                            print(
                                "subdomain contains at %s and %s"
                                % (str(part), str(domain_tld.domain))
                            )
                        subdomain_contains = True
                        break

                # Ex. microsoft.com is definitely not related to
                # espn.com, but espn.go.com is probably related to espn.com
                if not subdomain_contains and (child_tld.domain != domain_tld.domain):
                    if self.config.verbose:
                        print(("elim category url %s for domain mismatch" % p_url))
                        continue
                elif child_tld.subdomain in ["m", "i"]:
                    if self.config.verbose:
                        print(("elim category url %s for mobile subdomain" % p_url))
                    continue
                else:
                    valid_categories.append(scheme + "://" + domain)
                    # TODO account for case where category is in form
                    # http://subdomain.domain.tld/category/ <-- still legal!
            else:
                # we want a path with just one subdir
                # cnn.com/world and cnn.com/world/ are both valid_categories
                path_chunks = [x for x in path.split("/") if len(x) > 0]
                if "index.html" in path_chunks:
                    path_chunks.remove("index.html")

                if len(path_chunks) == 1 and len(path_chunks[0]) < 14:
                    valid_categories.append(domain + path)
                else:
                    if self.config.verbose:
                        print(
                            "elim category url %s for >1 path chunks "
                            "or size path chunks" % p_url
                        )

        _valid_categories = []

        # TODO Stop spamming urlparse and tldextract calls...

        for p_url in valid_categories:
            path = urls.get_path(p_url)
            subdomain = tldextract.extract(p_url).subdomain
            conjunction = path + " " + subdomain
            bad = False
            for badword in url_stopwords:
                if badword.lower() in conjunction.lower():
                    if self.config.verbose:
                        print(
                            "elim category url %s for subdomain contain stopword!"
                            % p_url
                        )
                    bad = True
                    break
            if not bad:
                _valid_categories.append(p_url)

        _valid_categories.append("/")  # add the root

        for i, p_url in enumerate(_valid_categories):
            if p_url.startswith("://"):
                p_url = "http" + p_url
                _valid_categories[i] = p_url

            elif p_url.startswith("//"):
                p_url = "http:" + p_url
                _valid_categories[i] = p_url

            if p_url.endswith("/"):
                p_url = p_url[:-1]
                _valid_categories[i] = p_url

        _valid_categories = list(set(_valid_categories))

        category_urls = [
            urls.prepare_url(p_url, source_url) for p_url in _valid_categories
        ]
        category_urls = [c for c in category_urls if c is not None]
        return category_urls

    def extract_tags(self, doc):
        if len(list(doc)) == 0:
            return set()
        elements = self.parser.css_select(doc, A_REL_TAG_SELECTOR)
        if not elements:
            elements = self.parser.css_select(doc, A_HREF_TAG_SELECTOR)
            if not elements:
                return set()

        tags = []
        for el in elements:
            tag = self.parser.getText(el)
            if tag:
                tags.append(tag)
        return set(tags)

    @property
    def top_node(self) -> lxml.html.Element:
        """Returns the top node of the article.
        calculate_best_node() must be called first

        Returns:
            lxml.html.Element: The top node containing the article text
        """
        return self.atricle_body_extractor.top_node

    @property
    def top_node_complemented(self) -> lxml.html.Element:
        """The cleaned version of the top node, without any divs, linkstuffing, etc

        Returns:
            lxml.html.Element: deepcopy version of the top node, cleaned
        """
        return self.atricle_body_extractor.top_node_complemented

    def calculate_best_node(
        self, doc: lxml.html.Element
    ) -> Optional[lxml.html.Element]:
        """Extracts the most probable top node for the article text
        based on a variety of heuristics

        Args:
            doc (lxml.html.Element): Root node of the document.
              The search starts from here.
              usually it's the html tag of the web page

        Returns:
            lxml.html.Element: the article top element
            (most probable container of the article text), or None
        """
        self.atricle_body_extractor.parse(doc)

        return self.atricle_body_extractor.top_node

    def get_videos(self, doc):
        return self.video_extractor.parse(doc)
