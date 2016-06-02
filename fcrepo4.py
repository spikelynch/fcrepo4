"""
A Pythonic interface to Fedora Commons 4

Repository - connection to an FCREPO

Resource -> path
         -> status (tombstones etc)
         -> access
         -> triples (RDF metadata)
         -> children (list of child resources)
         -> content (bytes)

r = repo.get(path)

for c in r.children:
    

"""

import requests, os.path, mimetypes, json, yaml, logging, re
from urllib.parse import urlparse
from rdflib import Graph, Literal, URIRef, Namespace, RDF
from rdflib.namespace import DC

logging.basicConfig(format="[%(name)s] %(levelname)s: %(message)s")

METHODS = {
    'GET': requests.get,
    'PUT': requests.put,
    'POST': requests.post,
    'PATCH': requests.patch,
    'DELETE': requests.delete,
    'HEAD': requests.head,
    'OPTIONS': requests.options,
#    'MOVE': requests.move,
#    'COPY': requests.copy
}

# the following are what the code uses as a serialisation format for
# RDF between the repository and the Resource objects: the first is
# the mime type requested of the server, the second is the rdflib parser
    
RDF_MIME = 'text/turtle'
RDF_PARSE = 'turtle'    

LDP_CONTAINS = 'http://www.w3.org/ns/ldp#contains'

DC_FIELDS = [
    'contributor',
    'coverage',
    'creator',
    'date',
    'description',
    'format',
    'identifier',
    'language',
    'publisher',
    'relation',
    'rights',
    'source',
    'subject',
    'title',
    'type'
    ]

LOGLEVELS = {
    'debug': logging.DEBUG,
    'info': logging.INFO,
    'warning': logging.WARNING,
    'error': logging.ERROR,
    'critical': logging.CRITICAL
    }

URL_CHUNK = 512

class Error(Exception):
    """Base class for exceptions.

    Attributes:
        message (str): the error message"""

    def __init__(self, message):
        self.message = message

        
class URIError(Error):
    """Error for malformed URIs.

    Attributes:
        message (str)
    """
    pass

class ConflictError(Error):
    """Error for conflicts: like trying to create a path which exists"""
    pass

class ResourceError(Error):
    """Base class for API/Resource errors.

    Attributes:
        uri (str) -- the uri of the resource
        response (requests.Response) -- the HTTP response
        status_code (int) -- the HTTP status returned by the request
        reason (str) -- the text version of the HTTP status code
        message (str) -- an error messsage
"""

    def __init__(self, uri, response, message):
        """Parameters:

        uri (str): the uri of the resource
        response (requests.Response): the HTTP response
        message (str): additional message from the code throwing the exception
        """
        self.uri = uri
        self.response = response
        self.status_code = response.status_code
        self.reason = response.reason
        self.message = message



class Repository(object):
    """Connection to an FC4 repository."""
    
    def __init__(self, config='config.yml', loglevel=logging.WARNING):
        """Store the uri, login and password"""
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(loglevel)
        configd = {}
        if type(config) == dict:
            self.logger.debug("config is a dict")
            configd = config
        else:
            configd = self.load_config(config)
        fields = [ 'uri', 'user', 'password' ]
        m = [ f for f in fields if f not in configd ]
        if m:
            message = "Config values missing: {}".format(', '.join(m))
            self.logger.critical(message)
            raise Error(message)
        if 'loglevel' in configd:
            if configd['loglevel'] in LOGLEVELS:
                self.logger.setLevel(LOGLEVELS[configd['loglevel']])
                self.logger.info("Log level set to '{}' by {}".format(configd['loglevel'], config))
            else:
                self.logger.error("Warning: config {} matches no log level".format(configd['loglevel']))
        self.uri = configd['uri']
        self.user = configd['user']
        self.password = configd['password']
        if self.uri[-1:] != '/':
            self.uri += '/'
        self.pathre = re.compile("^{}rest/(.*)$".format(self.uri))

    def load_config(self, conffile):
        cf = None
        message = ''
        with open(conffile) as cf:
            try:
                cf = yaml.load(cf)
            except yaml.YAMLError as exc:
                message = "YAML {} parse error: {}".format(conffile, exc)
                if hasattr(exc, 'problem_mark'):
                    mark = exc.problem_mark
                    message += "Error position: {}:{}".format(mark.line + 1, mark.column + 1)
        if not cf:
            self.logger.critical(message)
            raise Error(message)
        return cf

    def path2uri(self, path):
        """Converts a REST API path to a url"""
        uri = self.uri + 'rest'
        if not path:
            return uri
        if path[0] != '/':
            return uri + '/' + path
        return uri + path

    def uri2path(self, uri):
        """Converts a full uri to a REST path.

Throws an exception if the uri doesn't match this repository
"""
        m = self.pathre.match(uri)
        if m:
            return m.group(1)
        else:
            raise URIError("Path mismatch - couldn't parse {} to a path in {}".format(uri, self.uri))
        
        
    def api(self, uri, method='GET', headers=None, data=None):
        """
Generic api call with an HTTP method, target URL and headers, data (for
plain POST) or files (for file uploads)

Default method is GET.
"""
        self.uri2path(uri)  # safety check: throw URI error if it's bad
        if method in METHODS:
            m = METHODS[method]
            self.logger.debug("API {} {}".format(method, uri))
            if headers:
                self.logger.debug("headers={}".format(headers))
            if data:
                self.logger.debug("data={}".format(data))
            r = m(uri, auth=(self.user, self.password), headers=headers, data=data)
            return r
        else:
            return None

    def pathconcat(self, path, s):
        """Appends a suffix like fc:tombstone to a path"""
        if path[:-1] == '/':
            return path + s
        else:
            return path + '/' + s

    def dc_rdf(self, md):
        """A utility method for building a DC RDF graph from a dict"""
        g = Graph()

        obj = URIRef("")

        for field in DC_FIELDS:
            if field in md:
                g.add( (obj, DC[field], Literal(md[field])) )
        g.bind("dc", DC)
        return g

    def build_rdf(self, metadata, bind=None):
        """Takes a set of tuples and builds an RDF Graph object."""

        g = Graph()
        obj = URIRef("")
        for ( p, o ) in metadata:
            g.add((obj, p, o))
        if bind:
            for abbrev, namespace in bind.items():
                g.bind(abbrev, namespace)
        return g
        
    def get(self, uri, accept=None):
        """The basic method for retrieving a resource.

        Fetches the metadata for the resource at uri, raises a ResourceError
        if the status code was something other than ok
        """

        if accept:
            response = self.api(uri, headers = { 'Accept': accept })
        else:
            response = self.api(uri)
        if response.status_code == requests.codes.ok:
            resource = Resource(self, uri, response=response)
            if response.headers['Content-type'] == 'text/turtle':
                resource._parse_rdf(response.text)
            return resource
        else:
            message = "get {} returned HTTP status {} {}".format(uri, response.status_code, response.reason)
            raise ResourceError(uri, response, message)



    def add_container(self, uri, metadata, slug=None, path=None, force=False):
        """Add a new container inside an existing one.

        Parameters:
        uri (str) -- the path of the container to add to
        metadata (Graph) -- the RDF 
        path (str) -- path to new container, relative to uri
        slug (str) -- slug of new container
        force (boolean) -- where path is used, whether to force an overwrite

        Using the path parameter will try to create a deterministic path. If
        the path already exists and force is False (the default), a
        ConflictError is raised. If the path already exists and force is True,
        the existing path is deleted and obliterated and a new container is
        created.

        """
        rdf = metadata.serialize(format=RDF_MIME)
        headers = { 'Content-Type': RDF_MIME }
        if path:
            method = 'PUT'
            uri = self.pathconcat(uri, path)
            self._ensure_path(uri, force)
        else:
            method = 'POST'
            if slug:
                headers['Slug'] = slug
        resource = self._add_resource(uri, method, headers, rdf)
        resource.rdf = metadata
        return resource


    def add_binary(self, uri, source, slug=None, path=None, force=None, mime=None):
        """Upload binary data to a container.

        Parameters
        uri (str) -- the path of the container at which to add it
        metadata (Graph) -- RDF
        source (str, URI, file-like) -- a filename, URI or stream
        mime (str) -- MIME type
        slug (str) -- preferred id
        path (str) -- relative path from uri
        force (boolean) -- whether to overwrite path if it exists

        If no value is provided for path or slug, this method will try to
        use one from the filename or URI if possible: if not, it will let
        Fedora generate one.

        If a MIME-type is not provided, it's guessed from the filename, or
        taken from the URI. When passing in any other type of stream-like
        object, you should specify the MIME type: it will default to
        'application/octet-stream' otherwise. 
        """
        headers = {  }
        if path:
            method = 'PUT'
            uri = self.pathconcat(uri, path)
            self._ensure_path(uri, force)
            self.logger.debug("PUTting binary to {}".format(uri))
        else:
            method = 'POST'
            if slug:
                headers['Slug'] = slug
            self.logger.debug("POSTing binary to {} {}".format(uri, slug))

        if type(source) == str:
            if self._is_url(source):
                # open the source URL as a stream, then use the requests method
                # iter_content to get a generator which we pass to _add_resource
                # see http://docs.python-requests.org/en/master/user/advanced/
                source_r = requests.get(source, stream=True)
                headers['Content-type'] = source_r.headers['Content-type']
                basename = source.split('/')[-1]
                if method == 'POST' and slug:
                    basename = slug
                headers['Content-Disposition'] = 'attachment; filename="{}"'.format(basename)
                return self._add_resource(uri, method, headers, source_r.iter_content(URL_CHUNK))
                
            else:
                basename = os.path.basename(source)
                headers['Content-type'], _ = mimetypes.guess_type(source)
                headers['Content-Disposition'] = 'attachment; filename="{}"'.format(basename)
                with open(source, 'rb') as fh:
                    resource = self._add_resource(uri, method, headers, fh)
                return resource
        else:
            raise Error("add_binary only does files and URLs atm")

    def _is_url(self, source):
        """Tries to parse a data source string as a URL. If the result is
        a http or https URL, returns True.
        """
        p = urlparse(source)
        return p.scheme == 'http' or p.scheme == 'https'

        
    def _add_resource(self, uri, method, headers, data):
        """Internal method for PUT/POST: this does the error handling and
        builds the returned Resource object
        """
        response = self.api(uri, method=method, headers=headers, data=data)
        if response.status_code == requests.codes.created:
            uri = response.text
            return Resource(self, uri)
        else:
            message = "{} {} failed: {} {}".format(method, uri, response.status_code, response.reason)
            self.logger.error(message)            
            raise ResourceError(uri, response, message) 


        
    def _ensure_path(self, path, force):
        """Internal method to check if a path is free (and make sure it is
        if force is True. - this currently breaks if it's applied to a
        non-RDF path
        """
        response = None
        try:
            response = self.api(path)
        except ResourceError as re:
            if re.status_code == requests.codes.not_found:
                # not found is good
                self.logger.debug("Checked for {} - not found".format(path))
                pass
            else:
                raise re
        if response:
            if force:
                self.logger.debug("Force: obliterating {}".format(path))
                self.delete(path)
                self.obliterate(path)
            else:
                message = "Path {} already exists: can't re-create without force".format(path)
                self.logger.error(message)
                raise ConflictError(message)
    

        
    def _handle_data(self, source):
        """Take the data source passed to the add_binary function and turn it
        into a stream-like thing, if it isn't one.

        Parameters:
        source (str or file-like thing)

        Returns:
        a triple of ( mimetype (str), base name (str), stream (stream) )
        """

    def put(self, uri, metadata=None, data=None):
        """Basic method for PUT-ing metadata (or data) updates to a resource.

        Parameters:

        uri (str) - the resource's URI
        metadata (Graph) - the updated rdf
        data (string or file-like) - the updated payload

        Note that 'data' isn't implemented yet.

        Fedora checks for consistency when accepting an RDF update, so the
        RDF graph should be from a recent GET.
        """

        if metadata:
            rdf = metadata.serialize(format=RDF_MIME)
            headers = { 'Content-type': RDF_MIME }
            response = self.api(uri, method='PUT', headers=headers, data=rdf)
            if response.status_code == requests.codes.no_content:
                return True
            else:
                message = "put RDF {} returned HTTP status {} {}".format(uri, response.status_code, response.reason)
                raise ResourceError(uri, response, message)

        else:
            raise Error("Put to data objects has not been implemented")
    



        


    

    def delete(self, uri):
        """Deletes a resource"""
        return self._delete_uri(uri)

    def obliterate(self, uri):
        """Removes the tombstone record left by a resource"""
        tombstone = self.pathconcat(uri, 'fcr:tombstone')
        return self._delete_uri(tombstone)

    def _delete_uri(self, uri):
        response = self.api(uri, method="DELETE")
        if response.status_code == requests.codes.no_content:
            return True
        else:
            message = "delete {} returned HTTP status {} {}".format(uri, response.status_code, response.reason)
            raise ResourceError(uri, response, message)



class Resource(object):
    """Object representing a resource.

Attributes
    repo (Repository): the repository
    uri (str): its URI
    rdf (Graph): its RDF graph
    response (Response): the requests.Response object, if available

The methods on Resource objects mostly pass through to the corresponding
methods on its Repository object.
    """

    def __init__(self, repo, uri, metadata=None, response=None):
        """
Create a new Resource. Shouldn't be used by calling code - use the get and
children methods for that.

If the Resource was created by an http request, the requests.Response object
is stored (as 'response')
"""
        self.repo = repo
        self.uri = uri
        if metadata:
            if type(metadata) == Graph:
                self.rdf = metadata
            else:
                self.repo.logger.warning("Passed raw metadata to Resource")
                pass
        if response:
            self.response = response
        else:
            self.response = None

    def data(self):
        """Returns the data in the resource as a single lump"""
        if self.response:
            return self.response.text
        else:
            return None

    def stream(self):
        """Returns an object from which the data in the resource can be
        streamed"""
        if self.response:
            return self.response.raw
        else:
            return None
        
    def _parse_rdf(self, rdf):
        """Parse the serialised RDF content from FC as an rdflib Graph"""
        self.rdf = Graph()
        self.rdf.parse(data=rdf, format=RDF_PARSE)

    def bytes(self):
        """TBD - stream the resources' bytes"""
        pass

    def children(self):
        """Returns a list of paths of this resource's children"""
        return self.values(lambda p: p == LDP_CONTAINS)

    def rdf_search(self, predfilter):
        """Returns a list of all the objects where predfilter(p) is true"""
        return [ o for (_, p, o) in self.rdf if predfilter(p) ]

    def rdf_get_all(self, predicate):
        """Returns a list of all the objects with a predicate """
        return [ o for (_, p, o) in self.rdf if p == predicate ]

    def rdf_get(self, predicate):
        """Syntax sugar for get_all_rdf(p)[0]"""
        os = [ o for (_, p, o) in self.rdf if p == predicate ]
        if os:
            return os[0]
        else:
            return None

    def rdf_add(self, p, o):
        """Add a triple to the object's graph"""
        self.rdf.add(URIRef(""), p, o)

    def rdf_set(self, p, o):
        """This removes all triples with predicate matching p and then
        adds one a triple with predicate p and object o.
        """
        self.rdf.remove((URIRef(""), p, None))
        self.rdf.add((URIRef(""), p, o))

    # some extra methods for add_list and replace_list

    def dc(self):
        """Extracts all DC values and returns a dict"""
        dc = {}
        for field in DC_FIELDS:
            dc[field] = str(self.rdf_get(DC[field]))
        return dc
            
    
    def add_container(self, metadata, slug=None, path=None, force=False):
        """Add a new container to this resource.

        Parameters:
        metadata ([ (p, o) ]) -- a list of ( predicate, object ) tuples
        path (str) -- path to new container, relative to uri
        slug (str) -- slug of new container
        force (boolean) -- where path is used, whether to force an overwrite

        Using the path parameter will try to create a deterministic path. If
        the path already exists and force is False (the default), an error is
        raised. If the path already exists and force is True, the existing
        path is deleted and obliterated and a new, empty container is created.

        """
        return self.repo.add_container(self.uri, metadata, slug=slug, path=path, force=force)
        
    def add_binary(self, source, slug=None, path=None, force=False):
        """Add a new binary object to this resource.

        Parameters:
        source (str or file-like) -- an IO-style object, URI or filename
        path (str) -- path to new container, relative to uri
        slug (str) -- slug of new container
        force (boolean) -- where path is used, whether to force an overwrite

        The path, slug and force parameters have the same meaning as for
        add_container
        
        """
        return self.repo.add_binary(self.uri, source, slug=slug, path=path, force=force)

    def get(self):
        """Refreshes the RDF from Fedora"""
        self = self.repo.get(self.uri, accept=RDF_MIME)
    
    def put(self):
        """Writes the current RDF to Fedora"""
        if not self.rdf:
            raise Error("Resource at uri {} is not an RDF-resource".format(self.uri))
        self.repo.put(self.uri, metadata=self.rdf)
