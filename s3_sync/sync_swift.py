# Copyright 2017 SwiftStack
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from container_crawler.exceptions import RetryError
import eventlet
import hashlib
import json
import swiftclient
from swift.common.internal_client import UnexpectedResponse
from swift.common.utils import FileLikeIter, decode_timestamps
import sys
import traceback
import urllib

from .base_sync import BaseSync, ProviderResponse, match_item
from .utils import (ClosingResourceIterable, check_slo, DLO_ETAG_FIELD,
                    FileWrapper, get_dlo_prefix, iter_internal_listing,
                    MANIFEST_HEADER, SWIFT_USER_META_PREFIX)

# We have to import keystone upfront to avoid green threads issue with the lazy
# importer
try:
    from keystoneclient.v2_0 import client as _ks_v2_client  # NOQA
    from keystoneclient.v3 import client as _ks_v3_client  # NOQA
except ImportError:
    pass

DEFAULT_SEGMENT_DELAY = 60 * 60 * 24


class SyncSwift(BaseSync):
    def __init__(self, *args, **kwargs):
        super(SyncSwift, self).__init__(*args, **kwargs)
        # Used to verify the remote container in case of per_account uploads
        self.verified_container = False
        self.remote_delete_after = self.settings.get('remote_delete_after', 0)
        # Remote delete after addition for segments (defaults to 24 hours)
        self.remote_delete_after_addition = \
            self.settings.get('remote_delete_after_addition',
                              DEFAULT_SEGMENT_DELAY)

    @property
    def remote_container(self):
        if not self._per_account:
            return self.aws_bucket
        else:
            # In this case the aws_bucket is treated as a prefix
            return self.aws_bucket + self.container

    def remote_segments_container(self, container):
        if self._per_account:
            return self.aws_bucket + container
        else:
            return self.remote_container + '_segments'

    def _get_client_factory(self):
        # TODO: support LDAP auth
        username = self.settings['aws_identity']
        key = self.settings['aws_secret']
        # Endpoint must be defined for the Swift clusters and should be the
        # auth URL
        endpoint = self.settings['aws_endpoint']

        # **connection_args is common arguments; authurl, user, and key
        connection_kwargs = {'authurl': endpoint,
                             'user': username,
                             'key': key,
                             'retries': 3,
                             'os_options': {}}
        # default: swift, options: keystone_v2, and keystone_v3
        _authtype = self.settings.get('auth_type', 'swift')
        if _authtype == 'keystone_v2':
            if 'tenant_name' not in self.settings:
                raise ValueError(
                    'Missing tenant_name for keystone v2')
            connection_kwargs['tenant_name'] =\
                self.settings['tenant_name']
            connection_kwargs['auth_version'] = '2'
        elif _authtype == 'keystone_v3':
            _os_auth_fields = ['project_name',
                               'project_domain_name',
                               'user_domain_name']
            missing_fields = [field for field in _os_auth_fields
                              if field not in self.settings]
            if missing_fields:
                raise ValueError(
                    'Missing %s for keystone v3' %
                    ', '.join(missing_fields))
            connection_kwargs['os_options'] = dict(
                [(field, self.settings[field]) for field in _os_auth_fields])
            connection_kwargs['auth_version'] = '3'

        def swift_client_factory():
            _conn = swiftclient.client.Connection(**connection_kwargs)
            if self.settings.get('remote_account'):
                # We need to rewrite the account portion of the connection's
                # storage URL (NOTE: this wouldn't survive a
                # token-expiration-triggered re-auth if we didn't also jam the
                # resulting storage-url in
                # _conn.os_options['object_storage_url'])
                _conn.get_auth()
                scheme, rest = _conn.url.split(':', 1)
                host = urllib.splithost(rest)[0]
                path = '/v1/%s' % urllib.quote(
                    self.settings['remote_account'].encode('utf8'))
                storage_url = '%s:%s%s' % (scheme, host, path)
                _conn.url = storage_url
                _conn.os_options['object_storage_url'] = storage_url
            return _conn

        return swift_client_factory

    @staticmethod
    def _close_conn(conn):
        if conn.http_conn:
            conn.http_conn[1].request_session.close()

    def _client_headers(self, headers=None):
        headers = headers or {}
        headers.update(self.extra_headers)
        return headers

    def post_object(self, swift_key, headers, container=None):
        if not container:
            container = self.remote_container
        return self._call_swiftclient(
            'post_object', container, swift_key, headers=headers)

    def head_account(self):
        return self._call_swiftclient('head_account', None, None)

    def put_object(self, swift_key, headers, body_iter, query_string=None):
        return self._call_swiftclient(
            'put_object', self.remote_container, swift_key, contents=body_iter,
            headers=headers, query_string=query_string)

    def upload_object(self, row, internal_client):
        if self._per_account and not self.verified_container:
            with self.client_pool.get_client() as swift_client:
                try:
                    swift_client.head_container(self.remote_container,
                                                headers=self._client_headers())
                except swiftclient.exceptions.ClientException as e:
                    if e.http_status != 404:
                        raise
                    swift_client.put_container(self.remote_container,
                                               headers=self._client_headers())
            self.verified_container = True

        return self._upload_object(
            self.container, self.remote_container, row, internal_client,
            segment=False)

    def delete_object(self, swift_key):
        """Delete an object from the remote cluster.

        This is slightly more complex than when we deal with S3/GCS, as the
        remote store may have SLO manifests, as well. Because of that, this
        turns into HEAD+DELETE.
        """
        with self.client_pool.get_client() as swift_client:
            try:
                headers = swift_client.head_object(
                    self.remote_container, swift_key,
                    headers=self._client_headers())
            except swiftclient.exceptions.ClientException as e:
                if e.http_status == 404:
                    return
                raise

        delete_kwargs = {'headers': self._client_headers()}
        if check_slo(headers):
            delete_kwargs['query_string'] = 'multipart-manifest=delete'
        resp = self._call_swiftclient('delete_object',
                                      self.remote_container, swift_key,
                                      **delete_kwargs)
        if not resp.success and resp.status != 404:
            resp.reraise()
        return resp

    def shunt_object(self, req, swift_key):
        """Fetch an object from the remote cluster to stream back to a client.

        :returns: (status, headers, body_iter) tuple
        """
        headers_to_copy = ('Range', 'If-Match', 'If-None-Match',
                           'If-Modified-Since', 'If-Unmodified-Since')
        headers = {header: req.headers[header]
                   for header in headers_to_copy
                   if header in req.headers}
        headers['X-Trans-Id-Extra'] = req.environ['swift.trans_id']

        if req.method == 'GET':
            resp = self.get_object(
                swift_key, resp_chunk_size=65536, headers=headers)
        elif req.method == 'HEAD':
            resp = self.head_object(swift_key, headers=headers)
        else:
            raise ValueError('Expected GET or HEAD, not %s' %
                             req.method)
        return resp.to_wsgi()

    def shunt_post(self, req, swift_key):
        """Propagate metadata to the remote store

         :returns: (status, headers, body_iter) tuple
        """
        headers = dict([(k, req.headers[k]) for k in req.headers.keys()
                        if req.headers[k]])
        if swift_key:
            resp = self._call_swiftclient(
                'post_object', self.remote_container, swift_key,
                headers=headers)
        else:
            resp = self._call_swiftclient(
                'post_container', self.remote_container, None, headers=headers)
        return resp.to_wsgi()

    def shunt_delete(self, req, swift_key):
        """Propagate delete to the remote store

         :returns: (status, headers, body_iter) tuple
        """
        headers = dict([(k, req.headers[k]) for k in req.headers.keys()
                        if req.headers[k]])
        if not swift_key:
            resp = self._call_swiftclient(
                'delete_container', self.remote_container, None,
                headers=headers)
        else:
            resp = self._call_swiftclient(
                'delete_object', self.remote_container, swift_key,
                headers=headers)
        return resp.to_wsgi()

    def head_object(self, swift_key, bucket=None, **options):
        if bucket is None:
            bucket = self.remote_container
        resp = self._call_swiftclient('head_object', bucket, swift_key,
                                      **options)
        resp.body = ['']
        return resp

    def get_object(self, swift_key, bucket=None, **options):
        if bucket is None:
            bucket = self.remote_container
        return self._call_swiftclient(
            'get_object', bucket, swift_key, **options)

    def head_bucket(self, bucket=None, **options):
        if bucket is None:
            bucket = self.remote_container
        return self._call_swiftclient(
            'head_container', bucket, None, **options)

    def list_buckets(self, marker='', limit=None, prefix='', **kwargs):
        # NOTE: swiftclient does not currently support delimiter, so we ignore
        # it
        resp = self._call_swiftclient('get_account', None, None, marker=marker,
                                      prefix=prefix, limit=limit)
        if resp.status != 200:
            return resp
        for entry in resp.body:
            entry['content_location'] = self._make_content_location(
                entry['name'])
        return resp

    def _call_swiftclient(self, op, container, key, **args):
        def translate(header, value):
            if header.lower() in ('x-trans-id', 'x-openstack-request-id'):
                return ('Remote-' + header, value)
            if header == 'content-length':
                # Capitalize, so eventlet doesn't try to add its own
                return ('Content-Length', value)
            return (header, value)

        def _perform_op(client):
            try:
                if not container:
                    resp = getattr(client, op)(**args)
                elif container and not key:
                    resp = getattr(client, op)(container, **args)
                else:
                    resp = getattr(client, op)(container, key, **args)
                if not resp:
                    return ProviderResponse(True, 204, {}, [''])

                if isinstance(resp, tuple):
                    headers, body = resp
                else:
                    headers = resp
                    body = ['']
                if 'response_dict' in args:
                    headers = args['response_dict']['headers']
                    status = args['response_dict']['status']
                else:
                    status = 206 if 'content-range' in headers else 200
                    headers = dict([translate(header, value)
                                    for header, value in headers.items()])
                return ProviderResponse(True, status, headers, body)
            except swiftclient.exceptions.ClientException as e:
                headers = dict([translate(header, value)
                                for header, value in
                                e.http_response_headers.items()])
                return ProviderResponse(False, e.http_status, headers,
                                        iter(e.http_response_content),
                                        exc_info=sys.exc_info())
            except Exception:
                self.logger.exception('Error contacting remote swift cluster')
                return ProviderResponse(False, 502, {}, iter('Bad Gateway'),
                                        exc_info=sys.exc_info())

        args['headers'] = self._client_headers(args.get('headers', {}))
        # TODO: always use `response_dict` biz
        if op == 'get_object' and 'resp_chunk_size' in args:
            entry = self.client_pool.get_client()
            resp = _perform_op(entry.client)
            if resp.success:
                resp.body = ClosingResourceIterable(
                    entry, resp.body, resp.body.resp.close)
            else:
                resp.body = ClosingResourceIterable(
                    entry, resp.body, lambda: None)
            return resp
        else:
            if op == 'put_object':
                response_dict = args.get('response_dict', {})
                args['response_dict'] = response_dict
            with self.client_pool.get_client() as swift_client:
                return _perform_op(swift_client)

    def _upload_object(self, src_container, dst_container, row,
                       internal_client, segment=False):
        key = row['name']
        req_hdrs = {}
        if 'storage_policy_index' in row:
            req_hdrs['X-Backend-Storage-Policy-Index'] = row[
                'storage_policy_index']
        try:
            with self.client_pool.get_client() as swift_client:
                remote_meta = swift_client.head_object(
                    dst_container, key, headers=self._client_headers())
        except swiftclient.exceptions.ClientException as e:
            if e.http_status == 404:
                remote_meta = None
            else:
                raise

        try:
            metadata = internal_client.get_object_metadata(
                self.account, src_container, key,
                headers=req_hdrs)
        except UnexpectedResponse as e:
            if '404 Not Found' in e.message:
                return self.UploadStatus.NOT_FOUND
            raise

        if not segment:
            _, _, internal_timestamp = decode_timestamps(row['created_at'])
            if float(metadata['x-timestamp']) <\
                    float(internal_timestamp.internal):
                raise RetryError('Stale object %s' % key)

        if not segment and not match_item(metadata, self.selection_criteria):
            self.logger.debug(
                'Not archiving %s as metadata does not match: %s %s' % (
                    key, metadata, self.selection_criteria))
            return self.UploadStatus.SKIPPED_METADATA

        if check_slo(metadata):
            if segment:
                self.logger.warning(
                    'Nested SLOs are not currently supported. Failing to '
                    'upload: %s/%s/%s' % (self.account, src_container, key))
                return self.UploadStatus.SKIPPED_NESTED_SLO

            if remote_meta and self._check_slo_uploaded(
                    key, remote_meta, internal_client, req_hdrs):
                if not self._is_meta_synced(metadata, remote_meta):
                    self.update_metadata(key, metadata,
                                         remote_metadata=remote_meta,
                                         container=dst_container)
                    return self.UploadStatus.POST
                return self.UploadStatus.NOOP
            return self._upload_slo(key, internal_client, req_hdrs)

        dlo_prefix = get_dlo_prefix(metadata)
        if not segment and dlo_prefix:
            # TODO: we should be able to consolidate checking of uploaded
            # objects before getting into the specifics of uploading large
            # objects or regular objects.
            if remote_meta and self._check_dlo_uploaded(metadata, remote_meta,
                                                        internal_client):
                if not self._is_meta_synced(metadata, remote_meta):
                    self.update_metadata(key, metadata,
                                         remote_metadata=remote_meta,
                                         container=dst_container)
                    return self.UploadStatus.POST
                return self.UploadStatus.NOOP
            return self._upload_dlo(key, internal_client, metadata, req_hdrs)

        if remote_meta and metadata['etag'] == remote_meta['etag']:
            if not self._is_meta_synced(metadata, remote_meta):
                self.update_metadata(key, metadata,
                                     remote_metadata=remote_meta,
                                     container=dst_container)
                return self.UploadStatus.POST
            return self.UploadStatus.NOOP

        with self.client_pool.get_client() as swift_client:
            wrapper_stream = FileWrapper(internal_client,
                                         self.account,
                                         src_container,
                                         key,
                                         req_hdrs)
            headers = self._get_user_headers(wrapper_stream.get_headers())
            if self.remote_delete_after:
                del_after = self.remote_delete_after
                if segment:
                    del_after += self.remote_delete_after_addition
                headers.update({'x-delete-after': del_after})
            self.logger.debug('Uploading %s with meta: %r' % (
                key, headers))

            try:
                swift_client.put_object(
                    dst_container, key, wrapper_stream,
                    etag=wrapper_stream.get_headers()['etag'],
                    headers=self._client_headers(headers),
                    content_length=len(wrapper_stream))
            finally:
                wrapper_stream.close()
        return self.UploadStatus.PUT

    def _make_content_location(self, bucket):
        # If the identity gets in here as UTF8-encoded string (e.g. through the
        # verify command's CLI, if the creds contain Unicode chars), then it
        # needs to be upconverted to Unicode string.
        u_ident = self.settings['aws_identity'] if isinstance(
            self.settings['aws_identity'], unicode) else \
            self.settings['aws_identity'].decode('utf8')
        return '%s;%s;%s' % (self.endpoint, u_ident, bucket)

    def list_objects(self, marker, limit, prefix, delimiter=None,
                     bucket=None):
        if bucket is None:
            bucket = self.remote_container
        resp = self._call_swiftclient(
            'get_container', bucket, None,
            marker=marker, limit=limit, prefix=prefix, delimiter=delimiter)

        if not resp.success:
            return resp

        for entry in resp.body:
            entry['content_location'] = self._make_content_location(bucket)
        return resp

    def update_metadata(self, swift_key, metadata, remote_metadata={},
                        container=None):
        user_headers = self._get_user_headers(metadata)
        dlo_manifest = metadata.get(MANIFEST_HEADER, '').decode('utf-8')
        if dlo_manifest:
            # We have to preserve both our computed DLO ETag *and* the manifest
            # header.
            dlo_etag_field = SWIFT_USER_META_PREFIX + DLO_ETAG_FIELD
            segments_container, prefix = dlo_manifest.split('/', 1)
            user_headers[MANIFEST_HEADER] = '%s/%s' % (
                self.remote_segments_container(segments_container),
                prefix)
            user_headers[dlo_etag_field] = remote_metadata.get(dlo_etag_field)
        self.post_object(swift_key, user_headers, container)

    def _upload_objects(self, objects_list, internal_client):
        work_queue = eventlet.queue.Queue(self.SLO_QUEUE_SIZE)
        worker_pool = eventlet.greenpool.GreenPool(self.SLO_WORKERS)
        workers = []
        for _ in range(self.SLO_WORKERS):
            workers.append(
                worker_pool.spawn(
                    self._upload_segment_worker, work_queue, internal_client))
        # key must be a tuple (container, object)
        for key in objects_list:
            work_queue.put(key)
        work_queue.join()
        for _ in range(self.SLO_WORKERS):
            work_queue.put(None)
        errors = []
        for thread in workers:
            errors += thread.wait()
        return errors

    def _upload_slo(self, key, internal_client, swift_req_headers):
        headers, manifest = self._get_internal_manifest(
            key, internal_client, swift_req_headers)
        self.logger.debug("JSON manifest: %s" % str(manifest))

        errors = self._upload_objects(
            (segment['name'].split('/', 2)[1:] for segment in manifest),
            internal_client)

        # TODO: errors list contains the failed segments. We should retry
        # them on failure.
        if errors:
            raise RuntimeError('Failed to upload an SLO %s' % key)

        new_manifest = []
        for segment in manifest:
            segment_container, obj = segment['name'].split('/', 2)[1:]
            path = '/%s/%s' % (
                self.remote_segments_container(segment_container), obj)

            new_manifest.append(dict(path=path,
                                     etag=segment['hash'],
                                     size_bytes=segment['bytes']))

        self.logger.debug(json.dumps(new_manifest))
        manifest_hdrs = self._client_headers(self._get_user_headers(headers))
        if self.remote_delete_after:
            manifest_hdrs.update({'x-delete-after': self.remote_delete_after})
        # Upload the manifest itself
        with self.client_pool.get_client() as swift_client:
            swift_client.put_object(
                self.remote_container, key, json.dumps(new_manifest),
                headers=manifest_hdrs,
                query_string='multipart-manifest=put')
        return self.UploadStatus.PUT

    def _upload_dlo(self, key, internal_client, internal_meta, swift_req_hdrs):
        dlo_container, prefix = internal_meta[MANIFEST_HEADER].decode('utf-8')\
            .split('/', 1)
        dlo_etag_hash = hashlib.md5()

        def _keys_generator():
            internal_iterator = iter_internal_listing(
                internal_client, self.account, container=dlo_container,
                prefix=prefix)
            for entry in internal_iterator:
                if not entry:
                    return
                dlo_etag_hash.update(entry['hash'].strip('"'))
                yield (dlo_container, entry['name'])

        errors = self._upload_objects(_keys_generator(), internal_client)
        if errors:
            raise RuntimeError('Failed to upload a DLO %s' % key)
        dlo_etag = dlo_etag_hash.hexdigest()
        dlo_etag_header = SWIFT_USER_META_PREFIX + DLO_ETAG_FIELD

        # NOTE: if we can check whether any object has been uploaded, then we
        # could decide whether a new manifest needs to be PUT. For now, we PUT
        # it unconditionally.

        # Upload the manifest
        with self.client_pool.get_client() as swift_client:
            # The DLO manifest might be non-zero sized
            wrapper_stream = FileWrapper(internal_client,
                                         self.account,
                                         self.container,
                                         key, swift_req_hdrs)
            put_headers = self._get_user_headers(wrapper_stream.get_headers())
            put_headers[MANIFEST_HEADER] = '%s/%s' % (
                self.remote_segments_container(dlo_container), prefix)
            put_headers[dlo_etag_header] = dlo_etag
            try:
                swift_client.put_object(
                    self.remote_container, key, wrapper_stream,
                    etag=wrapper_stream.get_headers()['etag'],
                    headers=self._client_headers(put_headers),
                    content_length=len(wrapper_stream))
            finally:
                wrapper_stream.close()
        return self.UploadStatus.PUT

    def get_manifest(self, key, bucket=None):
        if bucket is None:
            bucket = self.remote_container
        with self.client_pool.get_client() as swift_client:
            try:
                headers, body = swift_client.get_object(
                    bucket, key,
                    query_string='multipart-manifest=get',
                    headers=self._client_headers())
                if 'x-static-large-object' not in headers:
                    return None
                return json.loads(body)
            except Exception as e:
                self.logger.warning('Failed to fetch the manifest: %s' % e)
                return None

    def _get_internal_manifest(self, key, internal_client, swift_headers={}):
        status, headers, body = internal_client.get_object(
            self.account, self.container, key, headers=swift_headers)
        if status != 200:
            body.close()
            raise RuntimeError('Failed to get the manifest')
        manifest = json.load(FileLikeIter(body))
        body.close()
        return headers, manifest

    def _upload_segment_worker(self, work_queue, internal_client):
        errors = []
        while True:
            work = work_queue.get()
            if not work:
                work_queue.task_done()
                return errors

            container, obj = work
            dst_container = self.remote_segments_container(container)
            try:
                self._upload_object(container, dst_container, {'name': obj},
                                    internal_client, segment=True)
            except swiftclient.exceptions.ClientException as e:
                # The segments may not exist, so we need to create it
                if e.http_status == 404:
                    self.logger.debug('Creating a segments container %s' %
                                      dst_container)
                    # Creating a container may take some (small) amount of time
                    # and we should attempt to re-upload in the following
                    # iteration
                    resp = self._call_swiftclient(
                        'put_container', dst_container)
                    if not resp.success:
                        self.logger.error(
                            'Failed to create segments container %s: %s' %
                            (dst_container, resp.to_wsgi()))
                    errors.append(work)
                else:
                    self.logger.error('Failed to upload segment %s/%s/%s: %s' %
                                      (self.account, container, obj,
                                       traceback.format_exc()))
            except:
                errors.append(work)
                self.logger.error('Failed to upload segment %s/%s/%s: %s' % (
                    self.account, container, obj, traceback.format_exc()))
            finally:
                work_queue.task_done()

    def _check_slo_uploaded(self, key, remote_meta, internal_client,
                            swift_req_hdrs):
        _, manifest = self._get_internal_manifest(
            key, internal_client, swift_req_hdrs)

        expected_etag = '"%s"' % hashlib.md5(
            ''.join([segment['hash'] for segment in manifest])).hexdigest()

        self.logger.debug('SLO ETags: %s %s' % (
            remote_meta['etag'], expected_etag))
        if remote_meta['etag'] != expected_etag:
            return False
        self.logger.info('Static large object %s has already been uploaded'
                         % key)
        return True

    def _check_dlo_uploaded(self, source_metadata, remote_metadata,
                            internal_client):
        # NOTE: DLOs do not have a stored ETag and no ETag returned at all if
        # the number of objects exceeds the first page of the response. We
        # compute and store our own ETag, which is the concatenation of all of
        # the ETags in the listing.
        digest = hashlib.md5()
        container, prefix = source_metadata[MANIFEST_HEADER].split('/', 1)
        iterator = iter_internal_listing(internal_client, self.account,
                                         container=container, prefix=prefix)
        for entry in iterator:
            if not entry:
                break
            digest.update(entry['hash'].strip('"'))
        dlo_etag = digest.hexdigest()

        remote_dlo_header = SWIFT_USER_META_PREFIX + DLO_ETAG_FIELD
        self.logger.debug('DLO ETags: %s %s' %
                          (remote_metadata.get(remote_dlo_header), dlo_etag))
        return remote_metadata.get(remote_dlo_header) == dlo_etag

    def post_container(self, metadata):
        return self._call_swiftclient(
            'post_container', self.remote_container, None, headers=metadata)

    @staticmethod
    def _is_meta_synced(local_metadata, remote_metadata):
        dlo_etag_header = SWIFT_USER_META_PREFIX + DLO_ETAG_FIELD
        remote_keys = [key.lower() for key in remote_metadata.keys()
                       if (key.lower().startswith(SWIFT_USER_META_PREFIX) and
                           # Remove the reserved DLO user-tag]
                           not key.lower() == dlo_etag_header)]
        local_keys = [key.lower() for key in local_metadata.keys()
                      if key.lower().startswith(SWIFT_USER_META_PREFIX)]
        if set(remote_keys) != set(local_keys):
            return False
        for key in local_keys:
            if local_metadata[key] != remote_metadata[key]:
                return False
        return True

    @staticmethod
    def _get_user_headers(all_headers):
        return dict([(key, value) for key, value in all_headers.items()
                     if key.lower().startswith(SWIFT_USER_META_PREFIX) or
                     key.lower() == 'content-type'])
