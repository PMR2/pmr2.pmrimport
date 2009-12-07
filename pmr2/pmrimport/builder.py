import re
import urllib, urllib2
import os, os.path
import logging
import time
import tempfile
from cStringIO import StringIO
from shutil import copy, copy2, copystat, rmtree

import lxml.etree
from mercurial import ui, hg, revlog, cmdutil, util

from constants import *


class Error(EnvironmentError):
    # see copytree
    pass


def get_pmr_urilist(filelisturi):
    """\
    Returns list of CellML files.
    """

    return urllib2.urlopen(filelisturi).read().split()

def prepare_logger(loglevel=logging.ERROR):
    formatter = logging.Formatter('%(message)s')
    logger = logging.getLogger('dirbuilder')
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(loglevel)

def create_workdir(d):
    if os.path.isdir(d):
        raise ValueError('destination directory already exists')
    try:
        os.mkdir(d)
    except OSError:
        raise ValueError('destination directory cannot be created')

def copytree(src, dst, symlinks=False):
    """Recursively copy a directory tree using copy2().

    The destination directory must not already exist.
    If exception(s) occur, an Error is raised with a list of reasons.

    If the optional symlinks flag is true, symbolic links in the
    source tree result in symbolic links in the destination tree; if
    it is false, the contents of the files pointed to by symbolic
    links are copied.

    XXX Consider this example code rather than the ultimate tool.
    XXX copied from shutil

    """
    names = os.listdir(src)
    # already created
    #os.makedirs(dst)
    errors = []
    for name in names:
        srcname = os.path.join(src, name)
        dstname = os.path.join(dst, name)
        try:
            if symlinks and os.path.islink(srcname):
                linkto = os.readlink(srcname)
                os.symlink(linkto, dstname)
            elif os.path.isdir(srcname):
                copytree(srcname, dstname, symlinks)
            else:
                copy2(srcname, dstname)
            # XXX What about devices, sockets etc.?
        except (IOError, os.error), why:
            errors.append((srcname, dstname, str(why)))
        # catch the Error from the recursive copytree so that we can
        # continue with other files
        except Error, err:
            errors.extend(err.args[0])
    try:
        copystat(src, dst)
    #except WindowsError:
    #    # can't copy file access times on Windows
    #    pass
    except OSError, why:
        errors.extend((src, dst, str(why)))
    if errors:
        raise Error, errors


class DownloadMonitor(object):
    """\
    A class that remembers downloads
    """

    def __init__(self):
        self.d = {}  # downloaded
        self.modified = {}

    def check(self, source, dest):
        return source in self.d and dest in self.d[source]

    def cached(self, source):
        fn = source in self.d and self.d[source][0] or None
        if fn is None:
            return fn
        # sometimes we have a StringIO in here instead
        if hasattr(fn, 'getvalue'):
            return fn.getvalue()
        try:
            o = open(fn)
            result = o.read()
            o.close()
        except:
            result = None
        return result
    
    def remember(self, source, dest):
        if not source in self.d:
            self.d[source] = []
        if not source in self.d[source]:
            self.d[source].append(dest)


class CellMLBuilder(object):
    """\
    Handles the downloading of CellML files, correction and fetching
    the resources referenced by it (also correcting uris of those
    resources).
    """

    # TODO
    # * search and destroy file:// URIs
    # * rdf node errors

    re_breakuri = re.compile(
        '^([a-zA-Z\-_]*(?:_[0-9]{4})?)_' \
        '(?:version([0-9]{2}))' \
        '(?:_(variant[0-9]{2}))?' \
        '(?:_(part[0-9]{2}))?$'
    )

    re_clean_name = re.compile('_version[0-9]{2}(.*)$')

    re_clean_rdfres = re.compile('.*(?:http://www.cellml.org/models/|file://).*[^#]$')
    re_clean_rdfres_id = re.compile('.*(?:http://www.cellml.org/models/|file://).*(#.*)$')
    re_curation = re.compile(':$', re.M)
    re_zero_curation = lambda self, x: self.re_curation.sub(':0', x)

    def __init__(self, workdir, uri, downloaded=None):
        self.uri = uri
        self.workdir = workdir
        self.log = logging.getLogger('dirbuilder')
        self.downloaded = downloaded
        self.result = {
            'cellml': None,
            'images': [],
            'session': None,
            'missing': [],
            'exists': [],
        }
        self.timestamp = 0

    def breakuri(self, baseuri):
        """\
        Breaks the Base URI down to the required fragments.
        """

        try:
            x, self.citation, self.version, self.variant, self.part, x = \
                self.re_breakuri.split(baseuri)
        except ValueError:
            raise ValueError("'%s' is an invalid base uri" % baseuri)
        return self.citation, self.version, self.variant, self.part

    def mkdir(self, *a):
        """\
        Creates a directory within the working directory.  If directory
        is already created nothing is done.
        """

        d = os.path.join(self.workdir, *a)
        # assumes parent dir already exists.
        if not os.path.isdir(d):
            os.mkdir(d)

    def download(self, source, dest, processor=None):
        """\
        Downloads data from source to destination.

        source -
            uri.
        dest -
            file name of destination.
            alternately, a file-like object may be supplied.
        processor -
            function or method to process results.
        """

        def write(data):
            # if data implements write (i.e. dom), use that instead.
            if hasattr(data, 'write'):
                data.write(d_fd, encoding='utf-8', xml_declaration=True)
            else:
                d_fd.write(data)

        data = None
        if self.downloaded:
            if self.downloaded.check(source, dest):
                self.log.debug('..CACHED %s -> %s', source, dest)
                return
            data = self.downloaded.cached(source)
            s_modified = self.downloaded.modified.get(source, None)

        if data is None:
            try:
                s_fd = urllib2.urlopen(source)
            except urllib2.HTTPError, e:
                if e.code >= 400:
                    self.result['missing'].append(source)
                    self.log.warning('HTTP %d on %s', e.code, source)
                return None

            data = s_fd.read()
            s_modified = s_fd.headers.getheader('Last-Modified')
            if self.downloaded:
                self.downloaded.modified[source] = s_modified
            s_fd.close()

        if processor:
            data = processor(data)

        if hasattr(dest, 'write'):
            # assume a valid stream object
            d_fd = dest
            write(data)
            # since destination is opened outside of this method, we
            # don't close it in here.
        else:
            if os.path.exists(dest):
                orig = open(dest).read()
                if data == orig:
                    # nothing else to do
                    return None
                self.log.warning('%s is different from %s, which exists!',
                    source, dest)
                self.result['exists'].append((dest, source))
                return None
            d_fd = open(dest, 'w')
            write(data)
            d_fd.close()

            # set timestamp.
            if s_modified:
                if hasattr(os, 'utime'):
                    s_modstp = time.mktime(time.strptime(s_modified, 
                        '%a, %d %b %Y %H:%M:%S %Z'))
                    os.utime(dest, (s_modstp, s_modstp))
                    if s_modstp > self.timestamp and dest.endswith('.cellml'):
                        self.timestamp = s_modstp
            else:
                self.log.warning('%s has no timestamp', source)


        # downloaded
        if self.downloaded:
            self.downloaded.remember(source, dest)

    def get_baseuri(self, uri):
        return uri.split('/').pop()

    # XXX these properties and proper usage are quite confusing.
    # please have my appologies.
    @property
    def cellml_download_uri(self):
        # used for actual download, because this one has been modified
        # to return the last-modified header.
        return self.uri + '/pmr_download'

    @property
    def cellml_filename(self):
        return self.defaultname + '.cellml'

    @property
    def session_filename(self):
        return self.defaultname + '.session.xml'

    @property
    def xul_filename(self):
        return self.defaultname + '.xul'

    def download_cellml(self):
        self.log.debug('.d/l cellml: %s', self.uri)
        dest = self.cellml_filename
        self.download(self.cellml_download_uri, dest, self.process_cellml)
        self.log.debug('.w cellml: %s', dest)

    failsuite = (
        # let's hear it for 4Suite's non-standard, non-anonymous 
        # anonymous id that tries to be an advertisement
        # should be 'rdf:about="rdf:#' because this is a PMR converter
        # and I had to work with (more like work around) 4Suite on PMR
        # so hacks like these were introduced.  Normalize everything
        # first before we convert everything to proper RDF blind nodes.
        ('rdfid', 
            re.compile('rdf:ID="#?http://4suite.org/rdf/anonymous/'),
            'rdf:about="rdf:#'
        ),
        ('failsuite', 
            re.compile('http://4suite.org/rdf/anonymous/'), 
            'rdf:#',
        ),
        # failsuite also made our CellML metadata reference an explicit
        # uri where there were none
        # <rdf:Description rdf:about="http://www.cellml.org/models/butera_rinzel_smith_1999_version01">
        ('absoulute rdf reference', 
            re.compile(
                'rdf:about="http://www.cellml.org/models/'
                '[^#"]*_[0-9]{4}_version[0-9]{2}[^#"]*'), 
            r'rdf:about="',
        ),
        # the rest without real filename, assuming they were originally
        # references to cmeta:id nodes but the rdf:about attributes did
        # not have the prepending # to signifify reference to cmeta:id.
        ('originalnotid', 
            re.compile('rdf:about="http://www.cellml.org/models/([^"#]*)"'),
            r'rdf:about="#\1"',
        ),
        # or they could be translated into file
        ('originalnotid file:///', 
            re.compile('rdf:about="file:///([^"#]*)"'),
            r'rdf:about="#\1"',
        ),
        # rdf:# fakereource (should be blind nodes) represented as literals?
        ('rdffakeresource',
            re.compile('>(rdf:#[^<]*)</[^>]*>'),
            r' rdf:resource="\1"/>',
        ),
        # should be 'rdf:about="#'
        ('rdfidid?',
            re.compile('rdf:ID="#*'),
            'rdf:about="#',
        ),
        # file:// do not belong online (the rest of them)
        ('file://',
            re.compile('="file://[^"]*(#[^"]*")'),
            r'="\1',
        ),
        # normalize nodes to rdf:#
        ('miscorrection',
            re.compile(
                '"(#[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}'
                '-[0-9a-f]{12})"'),
            r'"rdf:\1"',
        ),
        # remove the rest of the gunk
        ('miscorrection2',
            re.compile(
                'rdf:(about|resource)=".+(rdf:#[^"]*)"'),
            r'rdf:\1="\2"',
        ),
        # xmlbase (not just 4Suite)
        ('xmlbase',
            re.compile(' xml:base="[^"]*"'),
            '',
        ),
        # PCEnv absolute RDF fragments
        ('pcenv RDF:about',
            re.compile(' RDF:about="[^"]*.cellml"'),
            ' RDF:about=""',
        ),
    )

    def fix_failsuite(self, data):
        # Exorcise the remaining RDF/XML possessed by 4Suite.
        failures = self.failsuite
        for fail in failures:
            if fail[1].search(data):
                if '4suite' not in self.result:
                    self.result['4suite'] = []
                self.result['4suite'].append(fail[0])
                data = fail[1].sub(fail[2], data)
        return data

    # PCEnv absolute RDF fragments
    rdf_ns_fail = (
        re.compile('xmlns:rdf="[^"]*"'),
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"',
    )
    
    def fix_rdfnsfail(self, data):
        # Exorcise the remaining RDF/XML possessed by 4Suite.
        fail = self.rdf_ns_fail
        data2 = fail[0].sub(fail[1], data)
        if data != data2:
            self.result['rdfns'] = 1
            return data2
        return data

    def get_metadata(self):
        metadata = StringIO()
        self.download(self.uri + METADATA_FRAG, metadata)
        metadata.seek(0)
        return metadata

    def fix_missing_cellml_rdf(self, data):
        # XXX just replace here and not in failsuite because we need
        # this value correct to determine if we need to reapply RDF.
        data = self.fix_rdfnsfail(data)

        if '</rdf:RDF>' not in data[-200:]:
            # this checks whether we have processed RDF. if not, we
            # attempt to get it from the metadata part
            metadata = self.get_metadata()
            try:
                dom = lxml.etree.parse(metadata)
                paths = dom.xpath('/rdf:RDF/rdf:Description', 
                    namespaces={'rdf': 
                    'http://www.w3.org/1999/02/22-rdf-syntax-ns#'})
                if paths:
                    self.result['rdf'] = 'rdf re-appended from metadata'
                    # cheese
                    metadata_s = re.sub('<\?xml .*\?>', '',
                        metadata.getvalue(),
                    )
                    data = data.replace('</model>', metadata_s + '</model>')
                else:
                    if '<rdf:RDF' in data:
                        self.result['rdf'] = 'rdf was unprocessed'
                    else:
                        self.result['rdf'] = 'rdf is absent in file'
            except:
                self.result['rdf'] = 'rdf is absent in file and metadata ' \
                                     'from repo is broken'
        return data

    def process_cellml(self, data):

        data = self.fix_missing_cellml_rdf(data)
        data = self.fix_failsuite(data)

        try:
            dom = lxml.etree.parse(StringIO(data))
        except:
            # XXX maybe a debug flag.
            import pdb;pdb.set_trace()
            pass

        images = dom.xpath('.//tmpdoc:imagedata/@fileref',
            namespaces=CELLML_NSMAP)
        self.download_images(images)
        # update the dom nodes
        self.process_cellml_dom(dom)
        return dom

    def process_cellml_dom(self, dom):
        """\
        Updates the DOM to have correct relative links.
        """

        imagedata = dom.xpath('.//tmpdoc:imagedata',
            namespaces=CELLML_NSMAP)
        for i in imagedata:
            if 'fileref' in i.attrib:
                i.attrib['fileref'] = self.get_baseuri(i.attrib['fileref'])
        # XXX
        # fix remaining 4suite rdf corruption errors and file://

    def download_images(self, images):
        """\
        Downloads the images and returns the list of uri fragments.
        """
        for i in images:
            uri = urllib.basejoin(self.uri, i)
            dest = self.path_join(self.get_baseuri(uri))
            self.log.debug('..d/l image: %s', uri)
            self.download(uri, dest)
            self.log.debug('..w image: %s', dest)
            self.result['images'].append(dest)
        return images

    def path_join(self, *path):
        # XXX method name could use better distinction between local
        # paths vs URIs.
        return os.path.join(self.workdir, self.citation, self.version, *path)

    def prepare_path(self):
        """\
        This creates the base directory structure and returns the
        location of the destination of the CellML file.
        """

        # preparation
        self.baseuri = self.get_baseuri(self.uri)
        self.breakuri(self.baseuri)

        self.mkdir(self.citation)
        self.mkdir(self.citation, self.version)
        self.defaultname = self.path_join(
            self.re_clean_name.sub('\\1', self.baseuri))
        cellml_path = self.cellml_filename
        # derive filename from filesystem path of CellML file
        self.result['cellml'] = os.path.basename(cellml_path)
        return cellml_path

    def process_session(self, data):

        dom = lxml.etree.parse(StringIO(data))
        xulpath = dom.xpath('.//rdf:Description[@pcenv:externalurl]',
            namespaces=CELLML_NSMAP)
        # get and update the XUL file
        for en in xulpath:
            self.download_xul(en)

        rdfresource = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource'
        rdfabout = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about'
        nodes = dom.xpath('.//*[@rdf:resource]',
            namespaces=CELLML_NSMAP)
        nodes.extend(dom.xpath('.//*[@rdf:about]',
            namespaces=CELLML_NSMAP))
        nodes = list(set(nodes))
        for node in nodes:
            # XXX this is kind of a non-flexiable hack to accomodate
            # both attribute types.
            if rdfresource in node.attrib:
                rdfattr = rdfresource
            elif rdfabout in node.attrib:
                rdfattr = rdfabout
            else:
                continue

            s = node.attrib[rdfattr]

            if self.re_clean_rdfres.match(s):
                if self.re_clean_rdfres_id.match(s):
                    node.attrib[rdfattr] = self.re_clean_rdfres_id.sub(
                        self.result['cellml'] + '\\1', s)
                else:
                    node.attrib[rdfattr] = self.result['cellml']
        return dom

    def download_session(self):
        session_uri = self.get_session_uri()
        if not session_uri:
            return
        self.log.debug('..d/l session: %s', session_uri)
        self.result['session'] = self.session_filename
        self.download(session_uri, self.session_filename, self.process_session)
        self.log.debug('..w session: %s', self.session_filename)

    def download_xul(self, node):
        # Since this is the end step, going to combine the processing
        # of the URI of the node within here also.
        externalurl = '{http://www.cellml.org/tools/pcenv/}externalurl'
        xul_uri = node.attrib[externalurl]
        self.log.debug('..d/l xul: %s', xul_uri)
        self.download(xul_uri, self.xul_filename)
        self.log.debug('..w xul: %s', self.xul_filename)
        # correction (make relative path using os.path.basename)
        node.attrib[externalurl] = os.path.basename(self.xul_filename)

    def get_session_uri(self):
        # not making this a property because this fetches an external
        # uri, but I supposed results can be cached... but in the
        # interest of KISS (for me anyway)...
        session = StringIO()
        self.download(self.uri + PCENV_SESSION_FRAG, session)
        session = session.getvalue() or None
        if session:
            session = urllib.basejoin(self.uri, session)
        return session

    def download_curation(self):
        curation = StringIO()
        curation.write('pmr_curation_star:')
        self.download(self.uri + CURATION_LEVEL_FRAG, curation)
        curation.write('\npmr_pcenv_star:')
        self.download(self.uri + PCENV_CURATION_LEVEL_FRAG, curation)
        curation.write('\npmr_jsim_star:')
        self.download(self.uri + JSIM_CURATION_LEVEL_FRAG, curation)
        curation.write('\npmr_cor_star:')
        self.download(self.uri + COR_CURATION_LEVEL_FRAG, curation)
        curation.write('\n')
        curation_values = self.re_zero_curation(curation.getvalue())

        # yes, I know all variants will share this same file, but the
        # curation should be the same for all of them.
        curationpath = os.path.join(self.workdir, self.citation, 
                                    self.version + CURATION_FILENAME)
        curationfp = open(curationpath, 'w')
        # write to curation file
        curationfp.write(curation_values)
        curationfp.close()

    def generate_mapping(self):
        """\
        generates the mapping file to be used by the workspace builder.
        """

        mapping = os.path.join(self.workdir, self.citation, MAPPING_FILENAME)
        mappingfp = open(mapping, 'a')
        variant = self.variant is not None and self.variant or ''
        # silly hack to prepend variant.
        if variant:
            variant = '_' + variant
        mappingfp.write('%s,%s,%s,%s\n' % (
            self.version,
            variant,
            self.version,
            variant,
        ))
        mappingfp.close()

    def finalize(self):
        # set timestamp on directory
        if self.timestamp and hasattr(os, 'utime'):
            os.utime(self.path_join(), (self.timestamp, self.timestamp))

    def get_result(self, key):
        return self.result.get(key, None)

    def run(self):
        """\
        Processes the CellML URI in here.
        """

        self.prepare_path()
        self.download_cellml()
        self.download_session()
        self.download_curation()
        self.generate_mapping()
        self.finalize()
        return self.result


class DirBuilder(object):
    """\
    The class that will fetch the files from PMR.

    Each citation (name1_name2_name3_year) will be a directory, and each
    version/variant will also have its directory.  Files will be 
    downloaded along with all its dependencies.
    """

    def __init__(self, workdir, files=None, loglevel=logging.ERROR):
        self.workdir = workdir
        self.files = files
        self.filelisturi = CELLML_FILE_LIST
        prepare_logger(loglevel)
        self.log = logging.getLogger('dirbuilder')
        self.summary = {}
        self.downloaded = DownloadMonitor()

    def _run(self):
        """\
        Starts the process.  Will write to filesystem.
        """

        # create working dir
        create_workdir(self.workdir)

        if not self.files:
            self.log.info('Getting file list from "%s"...' % self.filelisturi)
            self.files = get_pmr_urilist(self.filelisturi)
            self.files.sort()  # in order please.
        else:
            self.log.info('File list already defined')
        self.log.info('Processing %d URIs...' % len(self.files))
        for i in self.files:
            processor = CellMLBuilder(self.workdir, i, self.downloaded)
            result = processor.run()
            self.summary[i] = result
            self.log.info('Processed: %s', i)
        return self.summary

    def print_summary(self):
        # currently only output summary of errors
        print ''
        print '-' * 72
        print 'Export from PMR complete.  Below are errors encountered.'
        for k, v in self.summary.iteritems():
            if v['missing'] or v['exists']:
                print 'In %s:' % k
                for i in v['missing']:
                    print 'Missing: %s' % i
                for i in v['exists']:
                    print 'Exists: %s - %s' % i

        failsuite_result = []
        for k, v in self.summary.iteritems():
            if '4suite' in v:
                failsuite_result.append(
                    '4Suite exorcised from: %s = [%s]' % 
                    (k, ', '.join(v['4suite'])))
        failsuite_result.sort()
        print '\n'.join(failsuite_result)

        for k, v in self.summary.iteritems():
            if 'rdf' in v:
                print 'RDF metadata info for %s : %s' % (k, v['rdf'])
            if 'rdfns' in v:
                print 'RDF namespace repaired for %s' % k

        print '-' * 72

    def run(self):
        try:
            self._run()
            self.print_summary()
        except ValueError, e:
            self.log.error('ERROR: %s' % e)
            return 2
        except KeyboardInterrupt, e:
            self.log.error('user aborted!')
            return 255
        return 0


class WorkspaceBuilder(object):
    """\
    The class that will faciliate the construction of the workspace
    directory structure.  Uses the directory structure generated by
    DirBuilder.
    """

    def __init__(self, source, dest, loglevel=logging.ERROR):
        self.source = source
        self.dest = dest
        prepare_logger(loglevel)
        self.log = logging.getLogger('dirbuilder')
        self.summary = {}
        # tuple will be written to another mapping file with values
        # from citation_version##_variant##
        # to revision + file
        self.mapping = []

    def list_models(self):
        result = os.listdir(self.source)
        result.sort()
        return result

    def build_hg(self, name):
        def mkmapping(mappings):
            result = {}
            for m in mappings.splitlines():
                src_v, src_f, dst_v, dst_f = m.split(',')
                if dst_v not in result:
                    result[dst_v] = {}
                if src_v not in result[dst_v]:
                    result[dst_v][src_v] = []
                result[dst_v][src_v].append((src_f, dst_f,))
            # need to sort this, so back to tuples
            mapping = []
            for (i, j) in result.items():
                j = j.items()
                j.sort()
                mapping.append((i, j,))
            mapping.sort()
            return mapping

        source = os.path.join(self.source, name)
        # Mercurial
        dest = os.path.join(self.dest, name)
        create_workdir(dest)
        u = ui.ui(interactive=False)
        repo = hg.repository(u, dest, create=1)

        # use mapping file.
        fp = open(os.path.join(source, MAPPING_FILENAME), 'r')
        mapping_str = fp.read()
        fp.close()

        # generate mapping dict
        mapping = mkmapping(mapping_str)

        for dst_v, src_map in mapping:
            self.log.debug('creating version %s', dst_v)
            premap = []

            # for each "version", map old files into new files
            for src_v, filemap in src_map:
                self.log.debug('copying files from %s to %s', src_v, dst_v)
                source = os.path.join(self.source, name, src_v)

                tmpdir = tempfile.mkdtemp(dir=self.temproot)
                copytree(source, tmpdir)
                # rename fragments
                for src_f, dst_f in filemap:
                    # rename cellml
                    # XXX I am lazy so I cook some copypasta
                    cellml_sf = '%s%s%s' % (name, src_f, '.cellml')
                    cellml_df = '%s%s%s' % (name, dst_f, '.cellml')
                    cellml_src_p = os.path.join(tmpdir, cellml_sf)
                    cellml_dst_p = os.path.join(tmpdir, cellml_df)
                    if os.path.exists(cellml_src_p):
                        # no reason why this does NOT exist
                        os.rename(cellml_src_p, cellml_dst_p)

                    sess_src_p = os.path.join(tmpdir, '%s%s%s' % 
                        (name, src_f, '.session.xml'))
                    sess_dst_p = os.path.join(tmpdir, '%s%s%s' % 
                        (name, dst_f, '.session.xml'))
                    if os.path.exists(sess_src_p):
                        # rename first
                        os.rename(sess_src_p, sess_dst_p)
                        # read
                        f = open(sess_dst_p)
                        session_content = f.read()
                        f.close()
                        # write
                        f = open(sess_dst_p, 'w')
                        # XXX potential danger, but this is a fairly
                        # plain RDF file with attribute values that are
                        # not named the same as some other elements in the
                        # RDF file, so it *should* be fine
                        f.write(session_content.replace(cellml_sf, cellml_df))
                        f.close()

                    premap.append((
                        '%s_version%s' % (name, ''.join((src_v, src_f,))),
                        '%s%s' % (name, dst_f),
                    ))
                # renaming done, dump stuff from temp to new
                copytree(tmpdir, dest)
                rmtree(tmpdir)

            # this picks the latest source
            st = os.stat(source)

            commit_mtime = time.ctime(st.st_mtime)
            u = ui.ui(interactive=False)
            u.pushbuffer()  # silence ui
            repo = hg.repository(u, dest)
            msg = 'committing version%s of %s' % (os.path.basename(dst_v), name)
            usr = 'pmr2.import <nobody@models.cellml.org>'
            manifest = repo.changectx(None).manifest()
            files = [i for i in os.listdir(dest)
                if i != '.hg' and i not in manifest]
            repo.add(files)
            self.log.debug('%d new file(s) added', len(files))
            repo.commit([], msg, usr, commit_mtime, force=True)
            self.log.debug(msg)

            # log mapping
            # XXX kind of redoing a lot of repo inits
            repo = hg.repository(u, dest)
            log_r = repo.changectx(None).node().encode('hex')
            # XXX curation map is repeated even though it's same
            # again picks the latest version
            curf = open(os.path.join(source + CURATION_FILENAME))
            cur = curf.read().replace('\n', ',')
            curf.close()
            for log_s, log_d in premap:
                self.mapping.append(' '.join([log_s, log_d, log_r, cur]))

            b = u.popbuffer().splitlines()
            for i in b:
                if i.startswith('nothing changed'):
                    self.log.warning('HG: %s in %s', i, dst_v)
                else:
                    self.log.debug('HG: %s', i)

    def _run(self):
        create_workdir(self.dest)
        self.log.info('Workspace root %s created', self.dest)
        roots = self.list_models()
        self.log.info('Processing %d model workspaces', len(roots))
        try:
            self.temproot = tempfile.mkdtemp()
            for r in roots:
                if r.startswith('.'):
                    continue
                self.build_hg(r)
        finally:
            # cleanup
            rmtree(self.temproot)

        f = open(os.path.join(self.dest, PMR_MAPPING_FILE), 'w')
        f.write('\n'.join(self.mapping))
        f.close()

    def run(self):
        self._run()
