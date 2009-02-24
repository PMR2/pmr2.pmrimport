import re
import urllib, urllib2
import os, os.path
import logging
import time
from cStringIO import StringIO
from shutil import copy, copy2, copystat

import lxml.etree
from mercurial import ui, hg, revlog, cmdutil, util

CELLML_FILE_LIST = 'http://www.cellml.org/models/list_txt'
CELLML_NSMAP = {
    'tmpdoc': 'http://cellml.org/tmp-documentation',
    'pcenv': 'http://www.cellml.org/tools/pcenv/',
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
}
PCENV_SESSION_FRAG = '/getPcenv_session_uri'
BAD_FRAG = [
    'attachment_download',
]


class _ui(ui.ui):
    """\
    Monkey patching ui so output is proprogated to the parent's buffer.

    intended side effect is all output (wanted or not) from the spawned
    repo objs will have its output dumped into main ui object's buffer.

    unwanted side effects is unknown, but it appears this buffer is for
    storing the output generated by mercurial only for some other piece
    of code, so this overloaded/patched usage here is probably apt.
    """

    # XXX naturally remove this if I can find where the desired result
    # is supported by mercurial
    def __init__(self, parentui=None, *a, **kw):
        oldui.__init__(self, parentui, *a, **kw)
        if parentui:
            self.buffers = parentui.buffers

    write_err = ui.ui.write

oldui = ui.ui
ui.ui = _ui


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
    # nice feature to have here is to provide caching and download
    # methods.

    def __init__(self):
        self.d = {}

    def check(self, source, dest):
        return source in self.d and dest in self.d[source]
    
    def remember(self, source, dest):
        if not source in self.d:
            self.d[source] = set()
        self.d[source].add(dest)


class CellMLBuilder(object):
    """\
    Handles the downloading of CellML files, correction and fetching
    the resources referenced by it (also correcting uris of those
    resources).
    """

    # TODO
    # * search and destroy file:// URIs
    # * make the session CellML file link correction more rigorous (1)

    re_breakuri = re.compile(
        '^([a-zA-Z\-_]*(?:_[0-9]{4})?)_' \
        '(?:version([0-9]{2}))' \
        '(?:_(variant[0-9]{2}))?' \
        '(?:_(part[0-9]{2}))?$'
    )

    re_clean_name = re.compile('_version[0-9]{2}(.*)$')

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

        if self.downloaded and self.downloaded.check(source, dest):
            self.log.debug('..CACHED %s -> %s', source, dest)
            return

        try:
            s_fd = urllib2.urlopen(source)
        except urllib2.HTTPError, e:
            if e.code >= 400:
                self.result['missing'].append(source)
                self.log.warning('HTTP %d on %s', e.code, source)
            return None

        data = s_fd.read()
        s_modified = s_fd.headers.getheader('Last-Modified')
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
    def cellml_download_uri2(self):
        # used for replacement
        return self.uri + '/download'

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

    def process_cellml(self, data):
        dom = lxml.etree.parse(StringIO(data))
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
        # XXX quick replace
        # TODO (1) should probably use the name of this session file
        # to generate the correct CellML filename that it should point
        # to.  Find all the nodes, correct all references to filenames
        # which should also correct file:// paths or session that points
        # to explicit versions.
        data = data.replace(self.cellml_download_uri2, self.result['cellml'])

        dom = lxml.etree.parse(StringIO(data))
        xulpath = dom.xpath('.//rdf:Description[@pcenv:externalurl]',
            namespaces=CELLML_NSMAP)
        # get and update the XUL file
        for en in xulpath:
            self.download_xul(en)
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
        self.finalize()
        return self.result
        # self.get_curation()


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

    def list_models(self):
        result = os.listdir(self.source)
        result.sort()
        return result

    def build_hg(self, name):
        source = os.path.join(self.source, name)
        # Mercurial
        dest = os.path.join(self.dest, name)
        create_workdir(dest)
        u = ui.ui(interactive=False)
        repo = hg.repository(u, dest, create=1)

        versions = [os.path.join(source, i) for i in os.listdir(source)]
        versions.sort()
        for vp in versions:
            self.log.debug('copying files from %s to %s', vp, dest)
            copytree(vp, dest)
            st = os.stat(vp)
            commit_mtime = time.ctime(st.st_mtime)
            u = ui.ui(interactive=False)
            u.pushbuffer()  # silence ui
            repo = hg.repository(u, dest)
            msg = 'committing version%s of %s' % (os.path.basename(vp), name)
            usr = 'pmr2.import <nobody@example.com>'
            manifest = repo.changectx(None).manifest()
            files = [i for i in os.listdir(dest)
                if i != '.hg' and i not in manifest]
            repo.add(files)
            self.log.debug('%d new file(s) added', len(files))
            repo.commit([], msg, usr, commit_mtime, force=True)
            self.log.debug(msg)

            b = u.popbuffer().splitlines()
            for i in b:
                if i.startswith('nothing changed'):
                    self.log.warning('HG: %s in %s', i, vp)
                else:
                    self.log.debug('HG: %s', i)

    def _run(self):
        create_workdir(self.dest)
        self.log.info('Workspace root %s created', self.dest)
        roots = self.list_models()
        self.log.info('Processing %d model workspaces', len(roots))
        for r in roots:
            self.build_hg(r)

    def run(self):
        self._run()
