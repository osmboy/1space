# -*- coding: UTF-8 -*-

"""
Copyright 2019 SwiftStack

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import base64
import boto3
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.vendored.requests.exceptions import RequestException
from container_crawler.exceptions import RetryError
from cStringIO import StringIO
import datetime
import hashlib
import json
import mock
from s3_sync.sync_s3 import SyncS3, prefix_from_rule, is_conflict, same_expiry
from s3_sync import utils
from swift.common import swob
from swift.common.internal_client import UnexpectedResponse
import unittest
from utils import FakeStream


class TestSyncS3(unittest.TestCase):
    boto_not_found = ClientError(
        dict(Error=dict(Code='NotFound', Message='Not found'),
             ResponseMetadata=dict(HTTPStatusCode=404, HTTPHeaders={})),
        'HEAD')

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def setUp(self, mock_boto3):
        self.mock_boto3_session = mock.Mock()
        self.mock_boto3_client = mock.Mock()

        mock_boto3.return_value = self.mock_boto3_session
        self.mock_boto3_session.client.return_value = self.mock_boto3_client

        self.aws_bucket = 'bucket'
        self.scratch_space = 'scratch'
        self.max_conns = 10
        self.sync_s3 = SyncS3({'aws_bucket': self.aws_bucket,
                               'aws_identity': 'identity',
                               'aws_secret': 'credential',
                               'account': 'account',
                               'container': 'container'},
                              max_conns=self.max_conns)
        self.logger = mock.Mock()
        self.sync_s3.logger = self.logger

    def tearDown(self):
        checked_levels = ['error', 'exception']
        for level in checked_levels:
            for call in getattr(self.logger, level).mock_calls:
                print call
            getattr(self.logger, level).assert_not_called()

    @mock.patch('s3_sync.sync_s3.SeekableFileLikeIter')
    def test_put_object(self, mock_seekable):
        key = 'key'
        s3_key = self.sync_s3.get_s3_name(key)

        body_iter = ['a', 'b', 'c']
        self.mock_boto3_client.put_object.return_value = {
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {
                    'x-amz-request-id': 'zzee',
                    'x-amz-id-2': 'feefee',
                    'x-amz-meta-joojoo': 'foofoo',
                    'content-length': '0',
                    'etag': '"feebie"',  # probably not realistic
                    'shim': 'jim',
                },
            },
            'Metadata': {},
        }

        resp = self.sync_s3.put_object(key, {'x-object-meta-jojo': 'b'},
                                       body_iter,
                                       # query_string is in the interface, but
                                       # ignored by this provider.
                                       query_string='zing=bing')

        self.assertTrue(resp.success)
        self.assertEqual(200, resp.status)
        self.assertEqual({
            'Remote-x-amz-request-id': 'zzee',
            'Remote-x-amz-id-2': 'feefee',
            'x-object-meta-joojoo': 'foofoo',
            'Content-Length': '0',
            'etag': 'feebie',
            'shim': 'jim',
        }, resp.headers)
        self.assertEqual([
            mock.call(body_iter),
        ], mock_seekable.mock_calls)
        self.assertEqual([
            mock.call(Body=mock_seekable.return_value, Bucket=self.aws_bucket,
                      ContentLength=None,
                      ContentType='application/octet-stream',
                      Key=s3_key, Metadata={'jojo': 'b'},
                      ServerSideEncryption='AES256'),
        ], self.mock_boto3_client.put_object.mock_calls)

    def test_prefix_from_rule(self):
        tests = [
            ({}, None),
            ({'Prefix': 'abc'}, 'abc'),
            ({'Prefix': 'abc', 'Filter': {'Tag': 'def'}}, 'abc'),
            ({'Prefix': 'abc', 'Filter': {'Prefix': 'def'}}, 'def'),
            ({'Filter': {'Filter': 'def'}}, None),
            ({'Filter': {'Prefix': 'def'}}, 'def'),
            ({'Filter': {'Tag': 'def'}}, None),
            ({'Prefix': 'abc', 'Filter': {'Tag': 'xyz', 'Prefix': 'def'}},
             'def'),
        ]
        for test_num, (rule, expected_result) in enumerate(tests):
            self.assertEqual(expected_result, prefix_from_rule(rule),
                             'Failed test case: %d' % (test_num,))

    def test_is_conflict(self):
        tests = [
            (('abc', {'Prefix': 'abc', 'Status': 'Enabled'}), True),
            (('xabc', {'Prefix': 'abc'}), False),
            (('abc', {'Prefix': 'abcx'}), False),
            (('abc', {'Prefix': 'xabc'}), False),
            (('abcx', {'Prefix': 'abc', 'Status': 'Enabled'}), True),
            (('abc', {'Prefix': '', 'Status': 'Enabled'}), True),
            (('abc', {'Prefix': 'abc', 'Status': 'Disabled'}), False),
            (('abcx', {'Prefix': 'abc', 'Status': 'Disabled'}), False),
            (('abc', {'Prefix': '', 'Status': 'Disabled'}), False),
            (('abc', {}), False),
        ]
        for test_num, (test_args, expected_result) in enumerate(tests):
            self.assertEqual(expected_result, is_conflict(*test_args),
                             'Failed test case: %d' % (test_num,))

    def test_same_expiry(self):
        tests = [
            (({}, 0), False),
            (({}, 1000), False),
            (({'Expiration': {}}, 0), False),
            (({'Expiration': {}}, 1000), False),
            (({'Expiration': {'Days': 0}}, 0), True),
            (({'Expiration': {'Days': 1000}}, 1000), True),
        ]
        for test_num, (test_args, expected_result) in enumerate(tests):
            self.assertEqual(expected_result, same_expiry(*test_args),
                             'Failed test case: %d' % (test_num,))

    def test_update_lifecycle_policy(self):
        # Simple case: add special to rules==[]
        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                },
                'Rules': [],
            }
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        expected_rule = {
            'Expiration': {'Days': 33},
            'Filter': {'Prefix': self.sync_s3.get_s3_name('')},
            'ID': self.sync_s3.get_s3_name(''),
            'Status': 'Enabled',
        }
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket, LifecycleConfiguration={
                'Rules': [expected_rule]})
        ], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)

    def test_update_lifecycle_policy_non_conflict(self):
        # Simple case: add special to rules==[<non-conflicting>]
        orig_rule = {
            'Expiration': {'Days': 900},
            'Filter': {'Prefix': 'xxxx/account/container2'},
            'ID': 'someid',
            'Status': 'Enabled'}
        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                },
                'Rules': [orig_rule],
            }
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        expected_rule = {
            'Expiration': {'Days': 33},
            'Filter': {'Prefix': self.sync_s3.get_s3_name('')},
            'ID': self.sync_s3.get_s3_name(''),
            'Status': 'Enabled',
        }
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket, LifecycleConfiguration={
                'Rules': [orig_rule, expected_rule]})
        ], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)

    def test_update_lifecycle_policy_same_rule(self):
        # Simple case: update special (change from controller)
        prefix = self.sync_s3.get_s3_name('')
        orig_rule = {
            'Expiration': {'Days': 900},
            'Filter': {'Prefix': prefix},
            'ID': 'someid',
            'Status': 'Enabled'}
        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                },
                'Rules': [orig_rule],
            }
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        expected_rule = {
            'Expiration': {'Days': 33},
            'Filter': {'Prefix': prefix},
            'ID': prefix,
            'Status': 'Enabled',
        }
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket, LifecycleConfiguration={
                'Rules': [expected_rule]})
        ], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)
        self.assertEqual(
            [mock.call('Existing rule on bucket (%s) will be overwritten:'
                       ' %s' % (self.aws_bucket, orig_rule)),
             mock.call('Created new lifecycle rule for bucket (%s): %s' %
                       (self.aws_bucket, expected_rule))],
            self.logger.info.mock_calls)
        self.logger.info.reset_mock()

    def test_update_lifecycle_policy_conflict_default(self):
        # Conflict with default rule for bucket
        orig_rule = {
            'Expiration': {'Days': 900},
            'Filter': {'Prefix': ''},
            'ID': 'someid',
            'Status': 'Enabled'}
        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                },
                'Rules': [orig_rule],
            }
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        self.assertEqual(
            [], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)
        self.assertEqual(
            [mock.call('Unable to set expire after due to conflicting rule on'
                       ' bucket (%s): %s' % (self.aws_bucket, orig_rule))],
            self.logger.error.mock_calls)
        self.logger.error.reset_mock()

    def test_update_lifecycle_policy_conflict_other(self):
        # Conflict with other rule for bucket
        prefix = self.sync_s3.get_s3_name('')
        orig_rule = {
            'Expiration': {'Days': 900},
            'Filter': {'Prefix': prefix[:8]},
            'ID': 'someid',
            'Status': 'Enabled'}
        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                },
                'Rules': [orig_rule],
            }
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        self.assertEqual(
            [], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)
        self.assertEqual(
            [mock.call('Unable to set expire after due to conflicting rule on'
                       ' bucket (%s): %s' % (self.aws_bucket, orig_rule))],
            self.logger.error.mock_calls)
        self.logger.error.reset_mock()

    def test_update_lifecycle_policy_no_initial_rule(self):
        # Never had lifecycleconfiguration case
        def kablooey(**kwargs):
            raise ClientError(
                dict(Error=dict(
                    Code='NoSuchLifecycleConfiguration',
                    Message='An error occurred (NoSuchLifecycleConfiguration) '
                            'when calling the GetBucketLifecycleConfiguration '
                            'operation: The lifecycle configuration does not '
                            'exist'),
                     ResponseMetadata=dict(HTTPStatusCode=500,
                                           HTTPHeaders={})),
                'GET')

        self.mock_boto3_client.get_bucket_lifecycle_configuration.\
            side_effect = kablooey
        self.mock_boto3_client.put_bucket_lifecycle_configuration.\
            return_value = {
                'ResponseMetadata': {
                    'HTTPStatusCode': 200,
                }
            }
        # 33 days
        self.sync_s3.settings['remote_delete_after'] = 33 * 60 * 60 * 24
        self.sync_s3._update_lifecycle_policy()
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket),
        ], self.mock_boto3_client.get_bucket_lifecycle_configuration.
            mock_calls)
        fake_rule = {
            'Expiration': {'Days': 30000},
            'Filter': {'Prefix': ''},
            'ID': 'fakerule',
            'Status': 'Disabled',
        }

        expected_rule = {
            'Expiration': {'Days': 33},
            'Filter': {'Prefix': self.sync_s3.get_s3_name('')},
            'ID': self.sync_s3.get_s3_name(''),
            'Status': 'Enabled',
        }
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket, LifecycleConfiguration={
                'Rules': [fake_rule]}),
            mock.call(Bucket=self.aws_bucket, LifecycleConfiguration={
                'Rules': [expected_rule]})
        ], self.mock_boto3_client.put_bucket_lifecycle_configuration.
            mock_calls)

    @mock.patch('s3_sync.sync_s3.SeekableFileLikeIter')
    def test_put_object_with_content_length(self, mock_seekable):
        key = 'key'
        s3_key = self.sync_s3.get_s3_name(key)

        body_iter = ['a', 'b', 'c']
        self.mock_boto3_client.put_object.return_value = {
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {
                    'x-amz-request-id': 'zzee',
                    'x-amz-id-2': 'feefee',
                    'x-amz-meta-joojoo': 'foofoo',
                    'content-length': '0',
                    'etag': '"feebie"',  # probably not realistic
                    'shim': 'jim',
                },
            },
            'Metadata': {},
        }

        resp = self.sync_s3.put_object(key, {
            'x-object-meta-jojo': 'b',
            'content-length': '2',  # can totally be "short" of the iter
            # query_string is in the interface, but ignored by this provider.
        }, body_iter, query_string='zing=bing')

        self.assertTrue(resp.success)
        self.assertEqual(200, resp.status)
        self.assertEqual({
            'Remote-x-amz-request-id': 'zzee',
            'Remote-x-amz-id-2': 'feefee',
            'x-object-meta-joojoo': 'foofoo',
            'Content-Length': '0',
            'etag': 'feebie',
            'shim': 'jim',
        }, resp.headers)
        self.assertEqual([
            mock.call(body_iter, length=2),
        ], mock_seekable.mock_calls)
        self.assertEqual([
            mock.call(Body=mock_seekable.return_value, Bucket=self.aws_bucket,
                      ContentLength=2,
                      ContentType='application/octet-stream',
                      Key=s3_key, Metadata={'jojo': 'b'},
                      ServerSideEncryption='AES256'),
        ], self.mock_boto3_client.put_object.mock_calls)

    @mock.patch('s3_sync.sync_s3.SeekableFileLikeIter')
    def test_put_object_no_encryption(self, mock_seekable):
        key = 'key'
        s3_key = self.sync_s3.get_s3_name(key)

        body_iter = 'sham jammer'
        self.mock_boto3_client.put_object.return_value = {
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {
                    'content-length': '0',
                },
            },
            'Metadata': {},
        }

        self.sync_s3.encryption = False
        resp = self.sync_s3.put_object(key, {
            'x-object-meta-jojo': 'b',
            'blah': 'blah',  # ignored
            'content-type': 'text/plain',
            # query_string is in the interface, but ignored by this provider.
        }, body_iter, query_string='zing=bing')

        self.assertTrue(resp.success)
        self.assertEqual(200, resp.status)
        self.assertEqual({
            'Content-Length': '0',
        }, resp.headers)
        self.assertEqual([], mock_seekable.mock_calls)
        self.assertEqual([
            mock.call(Body=body_iter, Bucket=self.aws_bucket,
                      ContentLength=len(body_iter),
                      ContentType='text/plain', Key=s3_key,
                      Metadata={'jojo': 'b'}),
        ], self.mock_boto3_client.put_object.mock_calls)

    @mock.patch('s3_sync.sync_s3.SeekableFileLikeIter')
    def test_put_object_with_str(self, mock_seekable):
        key = 'key'
        s3_key = self.sync_s3.get_s3_name(key)

        body_iter = 'sham jammer'
        self.mock_boto3_client.put_object.return_value = {
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {
                    'content-length': '0',
                },
            },
            'Metadata': {},
        }

        resp = self.sync_s3.put_object(key, {
            'x-object-meta-jojo': 'b',
            'blah': 'blah',  # ignored
            'content-type': 'text/plain',
            # query_string is in the interface, but ignored by this provider.
        }, body_iter, query_string='zing=bing')

        self.assertTrue(resp.success)
        self.assertEqual(200, resp.status)
        self.assertEqual({
            'Content-Length': '0',
        }, resp.headers)
        self.assertEqual([], mock_seekable.mock_calls)
        self.assertEqual([
            mock.call(Body=body_iter, Bucket=self.aws_bucket,
                      ContentLength=len(body_iter),
                      ContentType='text/plain', Key=s3_key,
                      Metadata={'jojo': 'b'}, ServerSideEncryption='AES256'),
        ], self.mock_boto3_client.put_object.mock_calls)

    @mock.patch('s3_sync.sync_s3.SeekableFileLikeIter')
    def test_put_object_with_unicode(self, mock_seekable):
        key = 'key'
        s3_key = self.sync_s3.get_s3_name(key)

        body_iter = u'sham\u062ajammer'
        self.mock_boto3_client.put_object.return_value = {
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {
                    'content-length': '0',
                },
            },
            'Metadata': {},
        }

        resp = self.sync_s3.put_object(key, {
            'x-object-meta-jojo': 'b',
            'blah': 'blah',  # ignored
            'content-type': 'text/plain',
            # query_string is in the interface, but ignored by this provider.
        }, body_iter, query_string='zing=bing')

        self.assertTrue(resp.success)
        self.assertEqual(200, resp.status)
        self.assertEqual({
            'Content-Length': '0',
        }, resp.headers)
        self.assertEqual([], mock_seekable.mock_calls)
        self.assertEqual([
            mock.call(Body=body_iter.encode('utf8'), Bucket=self.aws_bucket,
                      ContentLength=len(body_iter.encode('utf8')),
                      ContentType='text/plain', Key=s3_key,
                      Metadata={'jojo': 'b'}, ServerSideEncryption='AES256'),
        ], self.mock_boto3_client.put_object.mock_calls)

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_upload_new_object(self, mock_file_wrapper):
        key = 'key'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}

        wrapper = mock.Mock()
        wrapper.__len__ = lambda s: 0
        wrapper.get_s3_headers.return_value = {}
        wrapper.get_headers.return_value = {'etag': 'fabcabbeef'}
        mock_file_wrapper.return_value = wrapper
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        self.sync_s3.check_slo = mock.Mock()
        self.sync_s3.check_slo.return_value = False
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = {
            'content-type': 'test/blob',
            'x-timestamp': str(1e9)}

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        mock_file_wrapper.assert_called_with(mock_ic,
                                             self.sync_s3.account,
                                             self.sync_s3.container,
                                             key, swift_req_headers,
                                             stats_cb=None)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Body=wrapper,
            Metadata={},
            ContentLength=0,
            ContentMD5=base64.b64encode('fabcabbeef'.decode('hex')),
            ServerSideEncryption='AES256',
            ContentType='test/blob')

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_upload_object_without_encryption(self, mock_file_wrapper):
        key = 'key'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}

        self.sync_s3.endpoint = 'http://127.0.0.1:8080'

        wrapper = mock.Mock()
        wrapper.__len__ = lambda s: 0
        wrapper.get_s3_headers.return_value = {}
        wrapper.get_headers.return_value = {'etag': 'fabcabbeef'}
        mock_file_wrapper.return_value = wrapper
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        self.sync_s3.check_slo = mock.Mock()
        self.sync_s3.check_slo.return_value = False
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = {
            'content-type': 'test/blob',
            'x-timestamp': str(1e9)}

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        mock_file_wrapper.assert_called_with(mock_ic,
                                             self.sync_s3.account,
                                             self.sync_s3.container,
                                             key, swift_req_headers,
                                             stats_cb=None)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Body=wrapper,
            Metadata={},
            ContentLength=0,
            ContentMD5=base64.b64encode('fabcabbeef'.decode('hex')),
            ContentType='test/blob')

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_google_upload_encryption(self, mock_file_wrapper):
        key = 'key'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}

        self.sync_s3.endpoint = 'https://storage.googleapis.com'

        wrapper = mock.Mock()
        wrapper.__len__ = lambda s: 0
        wrapper.get_s3_headers.return_value = {}
        wrapper.get_headers.return_value = {'etag': 'fabcabbeef'}
        mock_file_wrapper.return_value = wrapper
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        self.sync_s3.check_slo = mock.Mock()
        self.sync_s3.check_slo.return_value = False
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = {
            'content-type': 'test/blob',
            'x-timestamp': str(1e9)}

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        mock_file_wrapper.assert_called_with(mock_ic,
                                             self.sync_s3.account,
                                             self.sync_s3.container,
                                             key, swift_req_headers,
                                             stats_cb=None)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Body=wrapper,
            Metadata={},
            ContentLength=0,
            ContentMD5=base64.b64encode('fabcabbeef'.decode('hex')),
            ContentType='test/blob')

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_encryption_option(self, mock_session):
        sync_s3 = SyncS3({'aws_bucket': 'bucket',
                          'aws_identity': 'id',
                          'aws_secret': 'key',
                          'account': 'account',
                          'container': 'container',
                          'encryption': True})
        self.assertTrue(sync_s3.encryption)

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_upload_unicode_object_name(self, mock_file_wrapper):
        key = 'monkey-\xf0\x9f\x90\xb5'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}

        wrapper = mock.Mock()
        wrapper.__len__ = lambda s: 0
        wrapper.get_s3_headers.return_value = {}
        wrapper.get_headers.return_value = {'etag': 'fabcabbeef'}
        mock_file_wrapper.return_value = wrapper
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        self.sync_s3.check_slo = mock.Mock()
        self.sync_s3.check_slo.return_value = False
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = {
            'content-type': 'test/blob',
            'x-timestamp': str(1e9)}

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        mock_file_wrapper.assert_called_with(mock_ic,
                                             self.sync_s3.account,
                                             self.sync_s3.container,
                                             key, swift_req_headers,
                                             stats_cb=None)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=u"356b9d/account/container/" + key.decode('utf-8'),
            Body=wrapper,
            Metadata={},
            ContentLength=0,
            ContentMD5=base64.b64encode('fabcabbeef'.decode('hex')),
            ContentType='test/blob',
            ServerSideEncryption='AES256')

    def test_upload_changed_meta(self):
        key = 'key'
        storage_policy = 42
        etag = '1234'
        swift_object_meta = {'x-object-meta-new': 'new',
                             'x-object-meta-old': 'updated',
                             'etag': etag,
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'old': 'old'},
            'ETag': '"%s"' % etag
        }
        self.mock_boto3_client.copy_object.return_value = {
            'CopyObjectResult': {'LastModified': '2009-10-28T22:32:00',
                                 'ETag': etag}}

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.copy_object.assert_called_with(
            CopySource={'Bucket': self.aws_bucket,
                        'Key': self.sync_s3.get_s3_name(key)},
            MetadataDirective='REPLACE',
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Metadata={'new': 'new', 'old': 'updated'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')

    def test_upload_changed_meta_no_encryption(self):
        key = 'key'
        storage_policy = 42
        etag = '1234'
        swift_object_meta = {'x-object-meta-new': 'new',
                             'x-object-meta-old': 'updated',
                             'etag': etag,
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'old': 'old'},
            'ETag': '"%s"' % etag
        }
        self.mock_boto3_client.copy_object.return_value = {
            'CopyObjectResult': {'LastModified': '2009-10-28T22:32:00',
                                 'ETag': etag}}
        self.sync_s3.endpoint = 'http://127.0.0.1:8080'

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.copy_object.assert_called_with(
            CopySource={'Bucket': self.aws_bucket,
                        'Key': self.sync_s3.get_s3_name(key)},
            MetadataDirective='REPLACE',
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Metadata={'new': 'new', 'old': 'updated'},
            ContentType='test/blob')

    def test_upload_changed_meta_google_encryption(self):
        key = 'key'
        storage_policy = 42
        etag = '1234'
        swift_object_meta = {'x-object-meta-new': 'new',
                             'x-object-meta-old': 'updated',
                             'etag': etag,
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'old': 'old'},
            'ETag': '"%s"' % etag
        }
        self.mock_boto3_client.copy_object.return_value = {
            'CopyObjectResult': {'LastModified': '2009-10-28T22:32:00',
                                 'ETag': etag}}
        self.sync_s3.endpoint = 'https://storage.googleapis.com'

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.copy_object.assert_called_with(
            CopySource={'Bucket': self.aws_bucket,
                        'Key': self.sync_s3.get_s3_name(key)},
            MetadataDirective='REPLACE',
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Metadata={'new': 'new', 'old': 'updated'},
            ContentType='test/blob')

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_upload_changed_meta_glacier(self, mock_file_wrapper):
        key = 'key'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        etag = '1234'
        swift_object_meta = {'x-object-meta-new': 'new',
                             'x-object-meta-old': 'updated',
                             'etag': etag,
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'old': 'old'},
            'ETag': '"%s"' % etag,
            'StorageClass': 'GLACIER',
            'ContentType': 'test/blob'
        }

        wrapper = mock.Mock()
        wrapper.__len__ = lambda s: 0
        wrapper.get_s3_headers.return_value = {'new': 'new', 'old': 'updated'}
        wrapper.get_headers.return_value = {'etag': 'fabcabbeef'}
        mock_file_wrapper.return_value = wrapper

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        mock_file_wrapper.assert_called_with(mock_ic,
                                             self.sync_s3.account,
                                             self.sync_s3.container,
                                             key, swift_req_headers,
                                             stats_cb=None)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Metadata={'new': 'new', 'old': 'updated'},
            Body=wrapper,
            ContentLength=0,
            ContentMD5=base64.b64encode('fabcabbeef'.decode('hex')),
            ServerSideEncryption='AES256',
            ContentType='test/blob')

    @mock.patch('s3_sync.sync_s3.FileWrapper')
    def test_upload_replace_object(self, mock_file_wrapper):
        key = 'key'
        storage_policy = 42
        swift_object_meta = {'x-object-meta-new': 'new',
                             'x-object-meta-old': 'updated',
                             'etag': 'baddbeef',
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'old': 'old'},
            'ETag': 1,
            'ContentType': 'application/stream'
        }

        wrapper = mock.Mock()
        wrapper.get_s3_headers.return_value = utils.convert_to_s3_headers(
            swift_object_meta)
        wrapper.get_headers.return_value = swift_object_meta
        wrapper.__len__ = lambda s: 42
        mock_file_wrapper.return_value = wrapper

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.put_object.assert_called_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(key),
            Metadata={'new': 'new', 'old': 'updated'},
            Body=wrapper,
            ContentLength=42,
            ContentMD5=base64.b64encode(
                swift_object_meta['etag'].decode('hex')),
            ServerSideEncryption='AES256',
            ContentType='test/blob')

    def test_upload_same_object(self):
        key = 'key'
        storage_policy = 42
        etag = '1234'
        swift_object_meta = {'x-object-meta-foo': 'foo',
                             'etag': etag,
                             'content-type': 'test/blob',
                             'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = swift_object_meta
        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'foo': 'foo'},
            'ETag': '"%s"' % etag,
            'ContentType': 'test/blob'
        }

        self.sync_s3.upload_object(
            {'name': key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.copy_object.assert_not_called()
        self.mock_boto3_client.put_object.assert_not_called()

    def test_delete_object(self):
        key = 'key'
        self.mock_boto3_client.delete_object.return_value = {
            'DeleteMarker': False,
            'VersionId': '',
        }

        self.sync_s3.delete_object(key)
        self.mock_boto3_client.delete_object.assert_has_calls([
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(key)),
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_manifest_name(
                          self.sync_s3.get_s3_name(key)))])

    def test_delete_missing_object(self):
        key = 'key'
        error = self.boto_not_found
        self.mock_boto3_client.delete_object.side_effect = error
        self.sync_s3.delete_object(key)
        self.mock_boto3_client.delete_object.assert_has_calls([
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(key)),
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_manifest_name(
                          self.sync_s3.get_s3_name(key)))])

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_s3_name(self, mock_session):
        test_data = [('AUTH_test', 'container', 'key'),
                     ('acct', 'cont', '\u062akey'),
                     ('swift', 'stuff', 'my/key')]
        for account, container, key in test_data:
            sync = SyncS3({'account': account,
                           'container': container,
                           'aws_bucket': 'bucket',
                           'aws_identity': 'identity',
                           'aws_secret': 'secret'})
            # Verify that the get_s3_name function is deterministic
            self.assertEqual(sync.get_s3_name(key),
                             sync.get_s3_name(key))
            s3_key = sync.get_s3_name(key)
            self.assertTrue(isinstance(s3_key, unicode))
            prefix, remainder = s3_key.split('/', 1)
            self.assertEqual(remainder, '/'.join((account, container, key)))
            self.assertTrue(len(prefix) and
                            len(prefix) <= SyncS3.PREFIX_LEN)
            # Check the prefix computation
            md5_prefix = hashlib.md5('%s/%s' % (account, container))
            expected_prefix = hex(long(md5_prefix.hexdigest(), 16) %
                                  SyncS3.PREFIX_SPACE)[2:-1]
            self.assertEqual(expected_prefix, prefix)

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_s3_name_custom_prefix(self, mock_session):
        test_data = [('AUTH_test', 'container', 'key'),
                     ('acct', 'cont', '\u062akey'),
                     ('swift', 'stuff', 'my/key')]
        prefixes = ['scary', 'scary/', 'scary/idea/', '', '/']
        for c_pref in prefixes:
            for account, container, key in test_data:
                sync = SyncS3({'account': account,
                               'container': container,
                               'aws_bucket': 'bucket',
                               'aws_identity': 'identity',
                               'custom_prefix': c_pref,
                               'aws_secret': 'secret'})
                # Verify that the get_s3_name function is deterministic
                self.assertEqual(sync.get_s3_name(key),
                                 sync.get_s3_name(key))
                s3_key = sync.get_s3_name(key)
                self.assertTrue(isinstance(s3_key, unicode))
                e_pref = c_pref.strip('/')
                if len(e_pref):
                    expected = e_pref + '/' + key
                else:
                    expected = key
                self.assertEqual(expected, s3_key)

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_signature_version(self, session_mock):
        config_class = 's3_sync.sync_s3.boto3.session.Config'
        with mock.patch(config_class) as conf_mock:
            SyncS3({'aws_bucket': self.aws_bucket,
                    'aws_identity': 'identity',
                    'aws_secret': 'credential',
                    'account': 'account',
                    'container': 'container'})
            conf_mock.assert_called_once_with(
                signature_version='s3v4',
                s3={'payload_signing_enabled': False})

        with mock.patch(config_class) as conf_mock:
            SyncS3({'aws_bucket': self.aws_bucket,
                    'aws_identity': 'identity',
                    'aws_secret': 'credential',
                    'account': 'account',
                    'container': 'container',
                    'aws_endpoint': 'http://test.com'})
            conf_mock.assert_called_once_with(signature_version='s3',
                                              s3={'addressing_style': 'path'})

        with mock.patch(config_class) as conf_mock:
            SyncS3({'aws_bucket': self.aws_bucket,
                    'aws_identity': 'identity',
                    'aws_secret': 'credential',
                    'account': 'account',
                    'container': 'container',
                    'aws_endpoint': 'http://s3.amazonaws.com'})
            conf_mock.assert_called_once_with(signature_version='s3v4',
                                              s3={'aws_chunked': True})

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_session_token_plumbing(self, session_mock):
        SyncS3({'aws_bucket': 'a_bucket',
                'aws_identity': 'an_identity',
                'aws_secret': 'a_credential',
                'aws_session_token': 'a_token',
                'account': 'an_account',
                'container': 'a_container'})
        session_mock.assert_called_once_with(
            aws_access_key_id='an_identity',
            aws_secret_access_key='a_credential',
            aws_session_token='a_token')

    def test_slo_upload(self):
        slo_key = 'slo-object'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * 2**20},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 5 * 2**20}]

        self.mock_boto3_client.head_object.side_effect = self.boto_not_found

        def get_metadata(account, container, key, headers):
            if key == slo_key:
                return {utils.SLO_HEADER: 'True',
                        'x-timestamp': str(1e9)}
            raise RuntimeError('Unknown key')

        def get_object(account, container, key, headers):
            if key == slo_key:
                return (200,
                        {utils.SLO_HEADER: 'True',
                         'x-timestamp': str(1e9)},
                        FakeStream(content=json.dumps(manifest)))
            raise RuntimeError('Unknown key!')

        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.side_effect = get_metadata
        mock_ic.get_object.side_effect = get_object
        self.sync_s3._upload_slo = mock.Mock()

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.head_object.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key))
        mock_ic.get_object_metadata.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)
        mock_ic.get_object.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)

    @mock.patch('s3_sync.sync_s3.boto3.session.Session')
    def test_google_set(self, mock_session):
        session = mock.Mock()
        mock_session.return_value = session

        client = mock.Mock()
        session.client.return_value = client
        client.delete_object.return_value = {
            'DeleteMarker': False,
            'VersionId': '',
        }

        sync = SyncS3({'aws_bucket': self.aws_bucket,
                       'aws_identity': 'identity',
                       'aws_secret': 'credential',
                       'account': 'account',
                       'container': 'container',
                       'aws_endpoint': SyncS3.GOOGLE_API})
        # Connections are instantiated on-demand, so we have to submit a
        # request to check the boto session and client arguments.
        sync.delete_object('object')
        session.client.assert_has_calls([
            mock.call('s3',
                      config=mock.ANY,
                      endpoint_url=SyncS3.GOOGLE_API),
            mock.call().meta.events.register(
                'before-call.s3', sync._add_extra_headers),
            mock.call().meta.events.unregister(
                'before-call.s3.PutObject', mock.ANY),
            mock.call().meta.events.unregister(
                'before-call.s3.UploadPart', mock.ANY),
            mock.call().meta.events.unregister(
                'before-parameter-build.s3.ListObjects', mock.ANY)])
        self.assertEqual(True, sync._google())
        client.delete_object.assert_has_calls([
            mock.call(Bucket=self.aws_bucket, Key=sync.get_s3_name('object')),
            mock.call(Bucket=self.aws_bucket,
                      Key=sync.get_manifest_name(sync.get_s3_name('object')))])

    def test_user_agent(self):
        boto3_ua = boto3.session.Session()._session.user_agent()
        endpoint_user_agent = {
            SyncS3.GOOGLE_API: 'CloudSync/5.0 (GPN:SwiftStack) %s' % (
                boto3_ua),
            'https://s3.amazonaws.com': None,
            None: None,
            'http://other.s3-clone.com': None
        }

        session_class = 's3_sync.sync_s3.boto3.session.Session'
        for endpoint, ua in endpoint_user_agent.items():
            settings = {'aws_bucket': self.aws_bucket,
                        'aws_identity': 'identity',
                        'aws_secret': 'credential',
                        'account': 'account',
                        'container': 'container',
                        'aws_endpoint': endpoint}
            with mock.patch(session_class) as mock_session:
                session = mock.Mock()
                session._session.user_agent.return_value = boto3_ua
                mock_session.return_value = session

                client = mock.Mock()
                session.client.return_value = client
                client.delete_object.return_value = {
                    'DeleteMarker': False,
                    'VersionId': '',
                }

                sync = SyncS3(settings)
                # Connections are only instantiated when there is an object to
                # process. delete() is the simplest call to mock, so do so here
                sync.delete_object('object')

                if endpoint == SyncS3.GOOGLE_API:
                    session.client.assert_has_calls(
                        [mock.call('s3',
                                   config=mock.ANY,
                                   endpoint_url=endpoint),
                         mock.call().meta.events.register(
                            'before-call.s3', sync._add_extra_headers),
                         mock.call().meta.events.unregister(
                            'before-call.s3.PutObject', mock.ANY),
                         mock.call().meta.events.unregister(
                            'before-call.s3.UploadPart', mock.ANY),
                         mock.call().meta.events.unregister(
                            'before-parameter-build.s3.ListObjects',
                            mock.ANY)])
                else:
                    session.client.assert_has_calls(
                        [mock.call('s3',
                                   config=mock.ANY,
                                   endpoint_url=endpoint),
                         mock.call().meta.events.register(
                            'before-call.s3', sync._add_extra_headers),
                         mock.call().meta.events.unregister(
                            'before-call.s3.PutObject', mock.ANY),
                         mock.call().meta.events.unregister(
                            'before-call.s3.UploadPart', mock.ANY)])
                called_config = session.client.call_args[1]['config']

                if endpoint and not endpoint.endswith('amazonaws.com'):
                    self.assertEqual({'addressing_style': 'path'},
                                     called_config.s3)
                else:
                    self.assertEqual('s3v4', called_config.signature_version)
                    self.assertEqual(
                        {'payload_signing_enabled': False}, called_config.s3)
                self.assertEqual(endpoint == SyncS3.GOOGLE_API,
                                 sync._google())

                self.assertEqual(ua, called_config.user_agent)
                client.delete_object.assert_has_calls([
                    mock.call(Bucket=settings['aws_bucket'],
                              Key=sync.get_s3_name('object')),
                    mock.call(Bucket=settings['aws_bucket'],
                              Key=sync.get_manifest_name(
                                  sync.get_s3_name('object')))])

    def test_google_slo_upload(self):
        self.sync_s3._google = lambda: True
        slo_key = 'slo-object'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * SyncS3.MB},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 200}]

        self.mock_boto3_client.head_object.side_effect = self.boto_not_found

        def get_metadata(account, container, key, headers):
            if key == slo_key:
                return {utils.SLO_HEADER: 'True',
                        'x-timestamp': str(1e9)}
            raise RuntimeError('Unknown key')

        def get_object(account, container, key, headers):
            if key == slo_key:
                return (200, {'etag': 'swift-slo-etag',
                              'content-type': 'test/blob',
                              'x-timestamp': str(1e9)},
                        FakeStream(content=json.dumps(manifest)))
            raise RuntimeError('Unknown key!')

        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.side_effect = get_metadata
        mock_ic.get_object.side_effect = get_object

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.head_object.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key))

        args, kwargs = self.mock_boto3_client.put_object.call_args_list[0]
        self.assertEqual(self.aws_bucket, kwargs['Bucket'])
        s3_name = self.sync_s3.get_s3_name(slo_key)
        self.assertEqual(s3_name, kwargs['Key'])
        self.assertEqual(5 * SyncS3.MB + 200, kwargs['ContentLength'])
        self.assertEqual(
            {utils.SLO_ETAG_FIELD: 'swift-slo-etag'},
            kwargs['Metadata'])
        self.assertEqual(utils.SLOFileWrapper, type(kwargs['Body']))

        args, kwargs = self.mock_boto3_client.put_object.call_args_list[1]
        self.assertEqual(self.aws_bucket, kwargs['Bucket'])
        self.assertEqual(
            self.sync_s3.get_manifest_name(s3_name), kwargs['Key'])
        self.assertEqual(manifest, json.loads(kwargs['Body']))

        mock_ic.get_object_metadata.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)
        mock_ic.get_object.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)

    def test_google_slo_metadata_update(self):
        self.sync_s3._google = lambda: True
        self.sync_s3._is_amazon = lambda: False
        s3_key = self.sync_s3.get_s3_name('slo-object')
        slo_key = 'slo-object'
        storage_policy = 42

        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * SyncS3.MB},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 200}]

        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {utils.SLO_ETAG_FIELD: 'swift-slo-etag'},
            'ContentType': 'test/blob'}
        self.mock_boto3_client.copy_object.return_value = {
            'CopyObjectResult': {'LastModified': '2009-10-28T22:32:00',
                                 'ETag': 'deadbeef'}}

        def get_metadata(account, container, key, headers):
            if key == slo_key:
                return {utils.SLO_HEADER: 'True',
                        'x-object-meta-foo': 'bar',
                        'content-type': 'test/blob',
                        'x-timestamp': str(1e9)}
            raise RuntimeError('Unknown key')

        def get_object(account, container, key, headers):
            if key == slo_key:
                return (200, {'etag': 'swift-slo-etag',
                              'x-object-meta-foo': 'bar',
                              utils.SLO_HEADER: 'True',
                              'content-type': 'test/blob',
                              'x-timestamp': str(1e9)},
                        FakeStream(content=json.dumps(manifest)))
            raise RuntimeError('Unknown key!')

        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.side_effect = get_metadata
        mock_ic.get_object.side_effect = get_object

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.mock_boto3_client.copy_object.assert_called_with(
            CopySource={'Bucket': self.aws_bucket,
                        'Key': s3_key},
            MetadataDirective='REPLACE',
            Bucket=self.aws_bucket,
            Key=s3_key,
            Metadata={utils.SLO_ETAG_FIELD: 'swift-slo-etag',
                      'foo': 'bar',
                      utils.SLO_HEADER: 'True'},
            ContentType='test/blob')

    def test_internal_slo_upload(self):
        slo_key = 'slo-object'
        slo_meta = {'x-object-meta-foo': 'bar', 'content-type': 'test/blob'}
        s3_key = self.sync_s3.get_s3_name(slo_key)
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 100},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 200}]

        self.mock_boto3_client.create_multipart_upload.return_value = {
            'UploadId': 'mpu-key-for-slo'}

        def upload_part(**kwargs):
            if kwargs['PartNumber'] == 1:
                return {'ETag': '"deadbeef"'}
            elif kwargs['PartNumber'] == 2:
                return {'ETag': '"beefdead"'}
            else:
                raise RuntimeError('Unknown call to upload part')

        def _get_object(*args, **kwargs):
            self.assertEqual(
                self.max_conns - 1, self.sync_s3.client_pool.free_count())
            path = '/'.join(args[1:3])
            for entry in manifest:
                if entry['name'][1:] == path:
                    break
            else:
                raise RuntimeError('unknown segment!')
            return 200, {'Content-Length': chunk_len,
                         'etag': entry['hash']}, fake_app_iter

        self.mock_boto3_client.upload_part.side_effect = upload_part

        chunk_len = 5 * SyncS3.MB
        fake_app_iter = FakeStream(chunk_len)
        mock_ic = mock.Mock()
        mock_ic.get_object.side_effect = _get_object
        self.sync_s3._upload_slo(manifest, slo_meta, s3_key, mock_ic)

        self.mock_boto3_client.create_multipart_upload.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key),
            Metadata={'foo': 'bar'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(slo_key),
                      PartNumber=1,
                      ContentLength=chunk_len,
                      ContentMD5=base64.b64encode(
                          manifest[0]['hash'].decode('hex')),
                      Body=mock.ANY,
                      UploadId='mpu-key-for-slo'),
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(slo_key),
                      PartNumber=2,
                      ContentLength=chunk_len,
                      ContentMD5=base64.b64encode(
                          manifest[1]['hash'].decode('hex')),
                      Body=mock.ANY,
                      UploadId='mpu-key-for-slo')
        ], self.mock_boto3_client.upload_part.mock_calls)
        self.mock_boto3_client.complete_multipart_upload\
            .assert_called_once_with(
                Bucket=self.aws_bucket,
                Key=self.sync_s3.get_s3_name(slo_key),
                UploadId='mpu-key-for-slo',
                MultipartUpload={'Parts': [
                    {'PartNumber': 1, 'ETag': 'deadbeef'},
                    {'PartNumber': 2, 'ETag': 'beefdead'}
                ]}
            )

    @mock.patch('s3_sync.sync_s3.traceback')
    def test_internal_slo_upload_failure(self, tb_mock):
        slo_key = 'slo-object'
        slo_meta = {'x-object-meta-foo': 'bar', 'content-type': 'test/blob'}
        s3_key = self.sync_s3.get_s3_name(slo_key)
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 100},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 200}]

        self.mock_boto3_client.create_multipart_upload.return_value = {
            'UploadId': 'mpu-key-for-slo'}

        self.mock_boto3_client.upload_part.side_effect = RuntimeError(
            'Failed to upload part')

        chunk_len = 5 * SyncS3.MB
        fake_app_iter = [
            (200, {'Content-Length': chunk_len,
                   'etag': manifest[0]['hash']}, FakeStream(chunk_len)),
            (200, {'Content-Length': chunk_len,
                   'etag': manifest[1]['hash']}, FakeStream(chunk_len))]
        mock_ic = mock.Mock()
        mock_ic.get_object.side_effect = fake_app_iter
        tb_mock.format_exc.return_value = 'traceback'

        with self.assertRaises(RuntimeError):
            self.sync_s3._upload_slo(
                manifest, slo_meta, s3_key, mock_ic)

        self.mock_boto3_client.create_multipart_upload.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key),
            Metadata={'foo': 'bar'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')
        self.assertEqual([
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(slo_key),
                      PartNumber=1,
                      ContentLength=chunk_len,
                      ContentMD5=base64.b64encode(
                          manifest[0]['hash'].decode('hex')),
                      Body=mock.ANY,
                      UploadId='mpu-key-for-slo'),
            mock.call(Bucket=self.aws_bucket,
                      Key=self.sync_s3.get_s3_name(slo_key),
                      PartNumber=2,
                      ContentLength=chunk_len,
                      ContentMD5=base64.b64encode(
                          manifest[1]['hash'].decode('hex')),
                      Body=mock.ANY,
                      UploadId='mpu-key-for-slo')
        ], self.mock_boto3_client.upload_part.mock_calls)
        self.mock_boto3_client.complete_multipart_upload\
            .assert_not_called()
        for _, _, body in fake_app_iter:
            self.assertTrue(body.closed)

        self.assertEqual(
            [mock.call('Failed to upload part %d for '
                       'account/segment_container/slo-object/part%d: %s' % (
                           i, i, 'traceback'))
             for i in range(1, 3)],
            self.logger.error.mock_calls)
        self.logger.error.reset_mock()

    def test_internal_slo_upload_encryption(self):
        slo_key = 'slo-object'
        slo_meta = {'x-object-meta-foo': 'bar', 'content-type': 'test/blob'}
        s3_key = self.sync_s3.get_s3_name(slo_key)
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 100}]

        self.mock_boto3_client.create_multipart_upload.return_value = {
            'UploadId': 'mpu-key-for-slo'}

        def upload_part(**kwargs):
            if kwargs['PartNumber'] == 1:
                return {'ETag': '"deadbeef"'}
            else:
                raise RuntimeError('Unknown call to upload part')

        self.mock_boto3_client.upload_part.side_effect = upload_part

        fake_app_iter = FakeStream(5 * SyncS3.MB)
        mock_ic = mock.Mock()
        mock_ic.get_object.return_value = (
            200, {'Content-Length': str(5 * SyncS3.MB),
                  'etag': manifest[0]['hash']}, fake_app_iter)

        self.sync_s3.encryption = True
        self.sync_s3._upload_slo(manifest, slo_meta, s3_key, mock_ic)

        self.mock_boto3_client.create_multipart_upload.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key),
            Metadata={'foo': 'bar'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')
        self.mock_boto3_client.upload_part.assert_called_once_with(
            Bucket=self.aws_bucket,
            Key=self.sync_s3.get_s3_name(slo_key),
            PartNumber=1,
            ContentLength=5 * SyncS3.MB,
            ContentMD5=base64.b64encode(manifest[0]['hash'].decode('hex')),
            Body=mock.ANY,
            UploadId='mpu-key-for-slo')

    @mock.patch('s3_sync.sync_s3.get_slo_etag')
    def test_slo_meta_changed(self, mock_get_slo_etag):
        slo_key = 'slo-object'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * SyncS3.MB},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 5 * SyncS3.MB}]

        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {},
            'ETag': '"etag-2"'}
        mock_get_slo_etag.return_value = 'etag-2'
        self.sync_s3.update_slo_metadata = mock.Mock()
        self.sync_s3._upload_slo = mock.Mock()
        slo_meta = {
            utils.SLO_HEADER: 'True',
            'x-object-meta-new-key': 'foo',
            'x-timestamp': str(1e9)
        }
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = slo_meta
        mock_ic.get_object.return_value = (
            200, slo_meta, FakeStream(content=json.dumps(manifest)))

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.sync_s3.update_slo_metadata.assert_called_once_with(
            slo_meta, manifest, self.sync_s3.get_s3_name(slo_key),
            swift_req_headers, mock_ic)
        self.assertEqual(0, self.sync_s3._upload_slo.call_count)
        mock_ic.get_object_metadata.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)
        mock_ic.get_object.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)

    @mock.patch('s3_sync.sync_s3.get_slo_etag')
    def test_slo_meta_update_glacier(self, mock_get_slo_etag):
        slo_key = 'slo-object'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * SyncS3.MB},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 5 * SyncS3.MB}]

        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {},
            'ETag': '"etag-2"',
            'StorageClass': 'GLACIER'}
        mock_get_slo_etag.return_value = 'etag-2'
        self.sync_s3.update_slo_metadata = mock.Mock()
        self.sync_s3._upload_slo = mock.Mock()
        slo_meta = {
            utils.SLO_HEADER: 'True',
            'x-object-meta-new-key': 'foo',
            'x-timestamp': str(1e9)}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = slo_meta
        mock_ic.get_object.return_value = (
            200, slo_meta, FakeStream(content=json.dumps(manifest)))

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.assertEqual(0, self.sync_s3.update_slo_metadata.call_count)
        self.sync_s3._upload_slo.assert_called_once_with(
            manifest, slo_meta, self.sync_s3.get_s3_name(slo_key), mock_ic,
            None)
        mock_ic.get_object_metadata.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)
        mock_ic.get_object.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)

    @mock.patch('s3_sync.sync_s3.get_slo_etag')
    def test_slo_no_changes(self, mock_get_slo_etag):
        slo_key = 'slo-object'
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}
        manifest = [{'name': '/segment_container/slo-object/part1',
                     'hash': 'deadbeef',
                     'bytes': 5 * 2**20},
                    {'name': '/segment_container/slo-object/part2',
                     'hash': 'beefdead',
                     'bytes': 5 * 2**20}]

        self.mock_boto3_client.head_object.return_value = {
            'Metadata': {'new-key': 'foo',
                         utils.SLO_HEADER: 'True'},
            'ETag': '"etag-2"',
            'ContentType': 'x-application/test'}
        mock_get_slo_etag.return_value = 'etag-2'
        self.sync_s3.update_slo_metadata = mock.Mock()
        self.sync_s3._upload_slo = mock.Mock()
        slo_meta = {
            utils.SLO_HEADER: 'True',
            'x-object-meta-new-key': 'foo',
            'content-type': 'x-application/test',
            'x-timestamp': str(1e9)
        }

        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.return_value = slo_meta
        mock_ic.get_object.return_value = (
            200, slo_meta, FakeStream(content=json.dumps(manifest)))

        self.sync_s3.upload_object(
            {'name': slo_key,
             'storage_policy_index': storage_policy,
             'created_at': str(1e9)}, mock_ic)

        self.sync_s3.update_slo_metadata.assert_not_called()
        self.sync_s3._upload_slo.assert_not_called()
        mock_ic.get_object_metadata.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)
        mock_ic.get_object.assert_called_once_with(
            'account', 'container', slo_key, headers=swift_req_headers)

    def test_slo_metadata_update(self):
        slo_meta = {
            utils.SLO_HEADER: 'True',
            'x-object-meta-new-key': 'foo',
            'x-object-meta-other-key': 'bar',
            'content-type': 'test/blob'
        }
        manifest = [
            {'name': '/segments/slo-object/part1',
             'hash': 'abcdef'},
            {'name': '/segments/slo-object/part2',
             'hash': 'fedcba'}]
        s3_key = self.sync_s3.get_s3_name('slo-object')
        segment_lengths = [12 * SyncS3.MB, 14 * SyncS3.MB]
        storage_policy = 42
        swift_req_headers = {'X-Backend-Storage-Policy-Index': storage_policy}

        def get_object_metadata(account, container, key, headers={}):
            return {'content-length': segment_lengths[int(key[-1]) - 1]}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.side_effect = get_object_metadata

        self.mock_boto3_client.create_multipart_upload.return_value = {
            'UploadId': 'mpu-upload'}

        def upload_part_copy(**kwargs):
            if kwargs['PartNumber'] == 1:
                return {'CopyPartResult': {'ETag': '"abcdef"'}}
            elif kwargs['PartNumber'] == 2:
                return {'CopyPartResult': {'ETag': '"fedcba"'}}
            raise RuntimeError('Invalid part!')

        self.mock_boto3_client.upload_part_copy.side_effect = upload_part_copy

        self.sync_s3.update_slo_metadata(slo_meta, manifest, s3_key,
                                         swift_req_headers, mock_ic)

        self.mock_boto3_client.create_multipart_upload.assert_called_once_with(
            Bucket=self.aws_bucket, Key=s3_key,
            Metadata={'new-key': 'foo', 'other-key': 'bar',
                      utils.SLO_HEADER: 'True'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')
        self.mock_boto3_client.upload_part_copy.assert_has_calls([
            mock.call(Bucket=self.aws_bucket, Key=s3_key, PartNumber=1,
                      CopySource={'Bucket': self.aws_bucket, 'Key': s3_key},
                      CopySourceRange='bytes=0-%d' % (12 * SyncS3.MB - 1),
                      UploadId='mpu-upload'),
            mock.call(Bucket=self.aws_bucket, Key=s3_key, PartNumber=2,
                      CopySource={'Bucket': self.aws_bucket, 'Key': s3_key},
                      CopySourceRange='bytes=%d-%d' % (
                          12 * SyncS3.MB,
                          26 * SyncS3.MB - 1),
                      UploadId='mpu-upload')
        ])
        self.mock_boto3_client.complete_multipart_upload\
            .assert_called_once_with(Bucket=self.aws_bucket, Key=s3_key,
                                     UploadId='mpu-upload',
                                     MultipartUpload={'Parts': [
                                         {'PartNumber': 1, 'ETag': 'abcdef'},
                                         {'PartNumber': 2, 'ETag': 'fedcba'}
                                     ]})
        mock_ic.get_object_metadata.assert_has_calls(
            [mock.call(self.sync_s3.account,
                       'segments',
                       'slo-object/part1',
                       headers=swift_req_headers),
             mock.call(self.sync_s3.account,
                       'segments',
                       'slo-object/part2',
                       headers=swift_req_headers)])

    def test_slo_metadata_update_encryption(self):
        slo_meta = {
            utils.SLO_HEADER: 'True',
            'x-object-meta-new-key': 'foo',
            'x-object-meta-other-key': 'bar',
            'content-type': 'test/blob'
        }
        manifest = [
            {'name': '/segments/slo-object/part1',
             'hash': 'abcdef'}]
        s3_key = self.sync_s3.get_s3_name('slo-object')
        segment_lengths = [12 * SyncS3.MB, 14 * SyncS3.MB]

        def get_object_metadata(account, container, key, headers):
            return {'content-length': segment_lengths[int(key[-1]) - 1]}
        mock_ic = mock.Mock()
        mock_ic.get_object_metadata.side_effect = get_object_metadata

        self.mock_boto3_client.create_multipart_upload.return_value = {
            'UploadId': 'mpu-upload'}

        def upload_part_copy(**kwargs):
            if kwargs['PartNumber'] == 1:
                return {'CopyPartResult': {'ETag': '"abcdef"'}}
            raise RuntimeError('Invalid part!')

        self.mock_boto3_client.upload_part_copy.side_effect = upload_part_copy

        self.sync_s3.update_slo_metadata(slo_meta, manifest, s3_key, {},
                                         mock_ic)

        self.mock_boto3_client.create_multipart_upload.assert_called_once_with(
            Bucket=self.aws_bucket, Key=s3_key,
            Metadata={'new-key': 'foo', 'other-key': 'bar',
                      utils.SLO_HEADER: 'True'},
            ServerSideEncryption='AES256',
            ContentType='test/blob')

    def test_validate_manifest_too_many_parts(self):
        segments = [{'name': '/segment/%d' % i} for i in xrange(10001)]
        self.assertEqual(
            False, self.sync_s3._validate_slo_manifest(segments))
        self.logger.error.assert_called_once_with(
            'Cannot upload a manifest with more than 10000 segments')
        self.logger.error.reset_mock()

    def test_validate_manifest_small_part(self):
        segments = [{'name': '/segment/1',
                     'bytes': 10 * SyncS3.MB,
                     'hash': 'deadbeef'},
                    {'name': '/segment/2',
                     'bytes': 10,
                     'hash': 'deadbeef'},
                    {'name': '/segment/3',
                     'bytes': '10',
                     'hash': 'deadbeef'}]
        self.assertEqual(
            False, self.sync_s3._validate_slo_manifest(segments))
        self.logger.error.assert_called_once_with(
            'SLO segment /segment/2 must be greater than %d MB' %
            (self.sync_s3.MIN_PART_SIZE / self.sync_s3.MB))
        self.logger.error.reset_mock()

    def test_validate_manifest_large_part(self):
        segments = [{'name': '/segment/1',
                     'hash': 'deadbeef',
                     'bytes': 10 * SyncS3.MB},
                    {'name': '/segment/2',
                     'hash': 'deadbeef',
                     'bytes': 10 * SyncS3.GB},
                    {'name': '/segment/3',
                     'hash': 'deadbeef',
                     'bytes': '10'}]
        self.assertEqual(
            False, self.sync_s3._validate_slo_manifest(segments))
        self.logger.error.assert_called_once_with(
            'SLO segment /segment/2 must be smaller than %d GB' %
            (self.sync_s3.MAX_PART_SIZE / self.sync_s3.GB))
        self.logger.error.reset_mock()

    def test_validate_manifest_small(self):
        segments = [{'name': '/segment/1',
                     'hash': 'abcdef',
                     'bytes': 10}]
        self.assertEqual(
            True, self.sync_s3._validate_slo_manifest(segments))

    def test_validate_manifest_range(self):
        segments = [{'name': '/segment/1',
                     'hash': 'abcdef',
                     'range': '102453-102462',
                     'bytes': 10}]
        self.assertEqual(
            False, self.sync_s3._validate_slo_manifest(segments))
        self.logger.error.assert_called_once_with(
            'Found unsupported "range" parameter for /segment/1 segment')
        self.logger.error.reset_mock()

    def test_is_object_meta_synced(self):
        # The structure for each entry is: swift meta, s3 meta, whether they
        # should be equal.
        test_metas = [({'x-object-meta-upper': 'UPPER',
                        'x-object-meta-lower': 'lower',
                        'content-type': 'test/blob'},
                       {'upper': 'UPPER',
                        'lower': 'lower'},
                       True),
                      ({'x-object-meta-foo': 'foo',
                        'x-object-meta-bar': 'bar',
                        'content-type': 'test/blob'},
                       {'foo': 'not foo',
                        'bar': 'bar'},
                       False),
                      ({'x-object-meta-unicode': '👍',
                        'x-object-meta-date': 'Wed, April 30 10:32:21 UTC',
                        'content-type': 'test/blob'},
                       {'unicode': '%F0%9F%91%8D',
                        'date': 'Wed%2C%20April%2030%2010%3A32%3A21%20UTC'},
                       True),
                      ({'x-object-meta-foo': 'foo',
                        'x-object-meta-bar': 'bar',
                        'x-static-large-object': 'True',
                        'content-type': 'test/blob'},
                       {'swift-slo-etag': 'deadbeef',
                        'x-static-large-object': 'True',
                        'foo': 'foo',
                        'bar': 'bar'},
                       True),
                      ({'x-static-large-object': 'True',
                        'content-type': 'test/blob'},
                       {'x-static-large-object': 'True'},
                       True),
                      # mismatch in content type should cause the object
                      # metadata to be update
                      ({'content-type': 'test/swift'},
                       {},
                       False)]
        for swift_meta, s3_meta, expected in test_metas:
            self.assertEqual(
                expected, SyncS3.is_object_meta_synced(
                    {'Metadata': s3_meta,
                     'ContentType': 'test/blob'}, swift_meta))

    def test_shunt_object(self):
        key = 'key'
        s3_name = self.sync_s3.get_s3_name(key)

        common_headers = {
            'content-type': 'application/unknown',
            'date': 'Thu, 15 Jun 2017 00:09:25 GMT',
            'last-modified': 'Wed, 14 Jun 2017 23:11:34 GMT',
            'server': 'Jetty(9.2.z-SNAPSHOT)',
            'x-amz-meta-mtime': '1497315527.000000'}
        common_response = {
            u'ContentType': 'application/unknown',
            u'Metadata': {'mtime': '1497315527.000000'}}

        tests = [
            dict(content='some fairly large content' * (1 << 16),
                 method='GET',
                 response={u'ETag': '"e06dd4228b3a7ab66aae5fbc9e4b905e"'},
                 headers={'etag': '"e06dd4228b3a7ab66aae5fbc9e4b905e"'},
                 conns_start=self.max_conns - 1),
            dict(content='',
                 method='GET',
                 response={u'ETag': '"d41d8cd98f00b204e9800998ecf8427e"'},
                 headers={'etag': '"d41d8cd98f00b204e9800998ecf8427e"'},
                 conns_start=self.max_conns - 1),
            dict(method='HEAD',
                 response={u'ETag': '"e06dd4228b3a7ab66aae5fbc9e4b905e"'},
                 headers={'etag': '"e06dd4228b3a7ab66aae5fbc9e4b905e"'},
                 conns_start=self.max_conns),
        ]

        for test in tests:
            body = test.get('content', '')
            http_headers = dict(common_headers)
            http_headers['content-length'] = str(len(body))
            http_headers.update(test['headers'])
            resp_meta = dict(
                HTTPHeaders=http_headers,
                HTTPStatusCode=200,
                RetryAttempts=0)
            req_response = dict(common_response)
            req_response.update(test['response'])
            req_response.update(dict(
                ResponseMetadata=resp_meta,
                ContentLength=len(body)))

            if 'content' in test:
                req_response[u'Body'] = StreamingBody(
                    StringIO(body), len(body))

            self.mock_boto3_client.reset_mock()
            mocked = getattr(self.mock_boto3_client,
                             '_'.join([test['method'].lower(), 'object']))
            mocked.return_value = req_response

            expected_headers = {}
            for k, v in common_headers.items():
                if k.startswith('x-amz-meta-'):
                    prop_name = k[len('x-amz-meta-'):]
                    expected_headers['x-object-meta-' + prop_name] = v
                else:
                    expected_headers[k] = v
            expected_headers['Content-Length'] = str(len(body))
            expected_headers['etag'] = test['headers']['etag'][1:-1]

            req = swob.Request.blank('/v1/AUTH_a/c/key', method=test['method'])
            status, headers, body_iter = self.sync_s3.shunt_object(req, key)
            self.assertEqual(
                test['conns_start'], self.sync_s3.client_pool.free_count())
            self.assertEqual(status.split()[0],
                             str(resp_meta['HTTPStatusCode']))
            self.assertEqual(sorted(headers), sorted(expected_headers.items()))
            self.assertEqual(b''.join(body_iter), body)
            mocked.assert_called_once_with(Bucket=self.aws_bucket, Key=s3_name)
            self.assertEqual(
                self.max_conns, self.sync_s3.client_pool.free_count())

    def test_shunt_object_includes_some_client_headers(self):
        key = 'key'
        body = 'some content'
        # minimal response
        head_response = {
            'ResponseMetadata': {
                'HTTPHeaders': {},
                'HTTPStatusCode': 304,
            }
        }
        get_response = {}
        get_response[u'Body'] = StringIO(body)
        get_response['ResponseMetadata'] = dict(
            HTTPHeaders={}, HTTPStatusCode=304)
        get_response['ResponseMetadata']['HTTPHeaders']['content-length'] = \
            len(body)
        self.mock_boto3_client.get_object.return_value = get_response
        self.mock_boto3_client.head_object.return_value = head_response

        req = swob.Request.blank('/v1/AUTH_a/c/key', method='GET', headers={
            'Range': 'r',
            'If-Match': 'im',
            'If-None-Match': 'inm',
            'If-Modified-Since': 'ims',
            'If-Unmodified-Since': 'ius',
        })
        status, headers, body_iter = self.sync_s3.shunt_object(req, key)
        self.assertEqual(status.split()[0], str(304))
        self.assertEqual(headers, [('Content-Length', 12)])
        self.assertEqual(b''.join(body_iter), body)
        self.assertEqual(self.mock_boto3_client.get_object.mock_calls,
                         [mock.call(Bucket=self.aws_bucket,
                                    Key=self.sync_s3.get_s3_name(key),
                                    Range='r',
                                    IfMatch='im',
                                    IfNoneMatch='inm',
                                    IfModifiedSince='ims',
                                    IfUnmodifiedSince='ius')])

        # Again, but with HEAD
        req.method = 'HEAD'
        status, headers, body_iter = self.sync_s3.shunt_object(req, key)
        self.assertEqual(status.split()[0], str(304))
        self.assertEqual(headers, [])
        self.assertEqual(b''.join(body_iter), b'')
        self.assertEqual(self.mock_boto3_client.head_object.mock_calls,
                         [mock.call(Bucket=self.aws_bucket,
                                    Key=self.sync_s3.get_s3_name(key),
                                    Range='r',
                                    IfMatch='im',
                                    IfNoneMatch='inm',
                                    IfModifiedSince='ims',
                                    IfUnmodifiedSince='ius')])

    def test_shunt_object_network_error(self):
        key = 'key'
        req = swob.Request.blank('/v1/AUTH_a/c/key', method='GET')
        tests = [{'method': 'GET',
                  'exception': RequestException,
                  'status': 502,
                  'headers': [('Content-Length', '11')],
                  'message': 'Bad Gateway',
                  'conns_start': self.max_conns - 1,
                  'conns_end': self.max_conns},
                 {'method': 'GET',
                  'exception': ClientError(
                      dict(Error=dict(Code='test error',
                                      Message='failure occurred'),
                           ResponseMetadata=dict(HTTPStatusCode=500,
                                                 HTTPHeaders={})),
                      'GET'),
                  'status': 500,
                  'message': 'failure occurred',
                  'headers': [('Content-Length', '16')],
                  'conns_start': self.max_conns - 1,
                  'conns_end': self.max_conns},
                 {'method': 'HEAD',
                  'exception': RequestException,
                  'status': 502,
                  'headers': [('Content-Length', '11')],
                  'message': b'',
                  'conns_start': self.max_conns - 1,
                  'conns_end': self.max_conns},
                 {'method': 'GET',
                  'exception': ClientError(
                      dict(Error=dict(
                           Code='NotFound', Message='Specified key not found'),
                           ResponseMetadata=dict(
                               HTTPStatusCode=404,
                               HTTPHeaders={'Content-Length': 1024})),
                      'GET'),
                  'status': 404,
                  'headers': [('Content-Length', '23')],
                  'message': 'Specified key not found',
                  'conns_start': self.max_conns - 1,
                  'conns_end': self.max_conns},
                 {'method': 'HEAD',
                  'exception': ClientError(
                      dict(Error=dict(
                           Code='NotFound', Message='Specified key not found'),
                           ResponseMetadata=dict(
                               HTTPStatusCode=404,
                               HTTPHeaders={'Content-Length': 1024})),
                      'GET'),
                  'status': 404,
                  'headers': [('Content-Length', '23')],
                  'message': b'',
                  'conns_start': self.max_conns,
                  'conns_end': self.max_conns}]

        for test in tests:
            req.method = test['method']
            mock_name = '_'.join([test['method'].lower(), 'object'])
            self.mock_boto3_client.reset_mock()
            mocked = getattr(self.mock_boto3_client, mock_name)
            mocked.reset_mock()
            mocked.side_effect = test['exception']

            status, headers, body_iter = self.sync_s3.shunt_object(req, key)
            if test['method'] == 'GET':
                self.assertEqual(test['conns_start'],
                                 self.sync_s3.client_pool.free_count())
            self.assertEqual(status.split()[0], str(test['status']))
            self.assertEqual(headers, test['headers'])
            self.assertEqual(b''.join(body_iter), test['message'])
            mocked.assert_called_once_with(Bucket=self.aws_bucket,
                                           Key=self.sync_s3.get_s3_name(key))
            self.assertEqual(test['conns_end'],
                             self.sync_s3.client_pool.free_count())
            if not isinstance(test['exception'], ClientError):
                self.logger.exception.assert_called_once_with(
                    "S3 API '%s' to %s/%s (key_id: %s): " % (
                        test['method'].lower() + '_object', 's3:/',
                        self.aws_bucket, 'identity'))
                self.logger.exception.reset_mock()

    def test_list_buckets(self):
        now_date = datetime.datetime.now()
        self.mock_boto3_client.list_buckets.return_value = {
            'Owner': [
                dict(DisplayName='test:tester',
                     ID='test:tester')
            ],
            'Buckets': [
                dict(CreationDate=now_date,
                     Name='bucket'),
                dict(CreationDate=now_date,
                     Name='test-bucket')
            ],
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {}
            }
        }
        resp = self.sync_s3.list_buckets()
        self.assertEqual(200, resp.status)
        self.assertEqual(
            dict(last_modified=now_date.strftime(utils.SWIFT_TIME_FMT),
                 count=0,
                 bytes=0,
                 name='bucket',
                 content_location='AWS S3'),
            resp.body[0])
        self.assertEqual(
            dict(last_modified=now_date.strftime(utils.SWIFT_TIME_FMT),
                 count=0,
                 bytes=0,
                 name='test-bucket',
                 content_location='AWS S3'),
            resp.body[1])

        resp = self.sync_s3.list_buckets(marker='test-bucket')
        self.assertEqual(200, resp.status)
        self.assertEqual([], resp.body)

    def test_list_buckets_error(self):
        self.mock_boto3_client.list_buckets.side_effect = RuntimeError(
            'Failed to list')
        resp = self.sync_s3.list_buckets()
        self.assertFalse(resp.success)
        self.assertEqual(502, resp.status)
        self.assertEqual('Bad Gateway', ''.join(resp.body))
        self.logger.exception.assert_called_once_with(
            "S3 API 'list_buckets' to s3:/ (key_id: identity): Failed to list")
        self.logger.exception.reset_mock()

    def test_list_objects(self):
        prefix = '%s/%s/%s' % (self.sync_s3.get_prefix(), self.sync_s3.account,
                               self.sync_s3.container)
        now_date = datetime.datetime.now()
        self.mock_boto3_client.list_objects.return_value = {
            'Contents': [
                dict(Key='%s/%s' % (prefix, 'barù'.decode('utf-8')),
                     ETag='"badbeef"',
                     Size=42,
                     LastModified=now_date),
                dict(Key='%s/%s' % (prefix, 'foo'),
                     ETag='"deadbeef"',
                     Size=1024,
                     LastModified=now_date)
            ],
            'CommonPrefixes': [
                dict(Prefix='%s/afirstpref' % prefix),
                dict(Prefix='%s/preflast' % prefix),
            ],
            'ResponseMetadata': {
                'HTTPStatusCode': 200,
                'HTTPHeaders': {}
            }
        }

        resp = self.sync_s3.list_objects('marker', 10, 'prefix', '-')
        self.mock_boto3_client.list_objects.assert_called_once_with(
            Bucket=self.aws_bucket,
            Prefix='%s/prefix' % prefix,
            Delimiter='-',
            MaxKeys=10,
            Marker='%s/marker' % prefix)
        self.assertEqual(200, resp.status)
        expected_location = 'AWS S3;%s;%s/' % (self.aws_bucket, prefix)
        self.assertEqual(
            dict(subdir='afirstpref',
                 content_location=expected_location),
            resp.body[0])
        self.assertEqual(
            dict(subdir='preflast',
                 content_location=expected_location),
            resp.body[3])
        self.assertEqual(
            dict(hash='badbeef',
                 name=u'bar\xf9',
                 bytes=42,
                 last_modified=now_date.isoformat(),
                 content_type='application/octet-stream',
                 content_location=expected_location),
            resp.body[1])
        self.assertEqual(
            dict(hash='deadbeef',
                 name='foo',
                 bytes=1024,
                 last_modified=now_date.isoformat(),
                 content_type='application/octet-stream',
                 content_location=expected_location),
            resp.body[2])

    def test_list_objects_error(self):
        self.mock_boto3_client.list_objects.side_effect = ClientError(
            dict(Error=dict(Code='ServerError', Message='failed to list'),
                 ResponseMetadata=dict(HTTPStatusCode=500, HTTPHeaders={})),
            'get_objects')
        prefix = '%s/%s/%s/' % (self.sync_s3.get_prefix(),
                                self.sync_s3.account, self.sync_s3.container)

        resp = self.sync_s3.list_objects('', 10, '', '')
        self.mock_boto3_client.list_objects.assert_called_once_with(
            Bucket=self.aws_bucket,
            Prefix=prefix,
            MaxKeys=10)
        self.assertEqual(500, resp.status)
        self.assertIn('failed to list', resp.body)

    def test_upload_object_head_failure(self):
        self.mock_boto3_client.head_object.side_effect = ClientError(
            dict(Error=dict(Code='ServerError', Message='failed to HEAD'),
                 ResponseMetadata=dict(HTTPStatusCode=500, HTTPHeaders={})),
            'head_object')

        with self.assertRaises(ClientError) as context:
            self.sync_s3.upload_object(
                {'name': 'key',
                 'storage_policy_index': '0',
                 'created_at': str(1e9)}, mock.Mock())

        self.assertIn('failed to HEAD',
                      context.exception.response['Error']['Message'])

    def test_upload_object_internal_client_head_failure(self):
        self.mock_boto3_client.head_object.return_value = {}
        mock_ic = mock.Mock(get_object_metadata=mock.Mock(
            side_effect=UnexpectedResponse('tragic error', None)))

        with self.assertRaises(UnexpectedResponse) as context:
            self.sync_s3.upload_object(
                {'name': 'key',
                 'storage_policy_index': 0,
                 'created_at': str(1e9)}, mock_ic)

        self.assertIn('tragic error', context.exception.message)

    def test_upload_object_boto_failure(self):
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        self.mock_boto3_client.put_object.side_effect = RuntimeError(
            'Failed to upload')
        object_body = FakeStream(1024)
        mock_ic = mock.Mock(
            get_object_metadata=mock.Mock(return_value={
                'content-type': 'application/test',
                'x-timestamp': str(1e9)}),
            get_object=mock.Mock(return_value=(
                200, {'Content-Length': len(object_body),
                      'etag': 'feedbadd'},
                object_body)))

        with self.assertRaises(RuntimeError):
            self.sync_s3.upload_object(
                {'name': 'key',
                 'storage_policy_index': '0',
                 'created_at': str(1e9)}, mock_ic)

        self.assertTrue(object_body.closed)

    def test_retry_error_stale_object(self):
        self.mock_boto3_client.head_object.return_value = {}
        mock_ic = mock.Mock(get_object_metadata=mock.Mock(
            return_value={'x-timestamp': 1e9}))

        with self.assertRaises(RetryError):
            self.sync_s3.upload_object(
                {'name': 'key',
                 'storage_policy_index': 0,
                 'created_at': str(2e9)}, mock_ic)

    def test_lifecycle_skip_selection_criteria(self):
        '''Should not lifecycle objects if metadata does not match.'''
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        mock_ic = mock.Mock(get_object_metadata=mock.Mock(
            return_value={'x-object-meta-foo': 'False',
                          'x-timestamp': 1e9}))
        self.sync_s3.selection_criteria = {
            'AND': [{'x-object-meta-foo': 'True'},
                    {'x-object-meta-bar': 'False'}]}

        self.assertEqual(SyncS3.UploadStatus.SKIPPED_METADATA,
                         self.sync_s3.upload_object(
                             {'name': 'key',
                              'storage_policy_index': 0,
                              'created_at': str(1e9)}, mock_ic))

    def test_lifecycle_match_selection_criteria(self):
        '''Should lifecycle objects with matching metadata.'''
        object_meta = {u'x-object-meta-fo\u00f4'.encode('utf-8'): 'True',
                       u'x-object-meta-b\u00e4r'.encode('utf-8'): 'False',
                       'x-timestamp': 1e9,
                       'Content-Length': '1024',
                       'etag': 'deadbeef',
                       'content-type': 'applcation/unknown'}
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found
        mock_ic = mock.Mock(
            get_object_metadata=mock.Mock(return_value=object_meta),
            get_object=mock.Mock(
                return_value=(200, object_meta, FakeStream())))
        self.sync_s3.selection_criteria = {
            'AND': [{u'x-object-meta-fo\u00d4': 'True'},
                    {u'x-object-meta-b\u00c4r': 'False'}]}

        self.assertEqual(SyncS3.UploadStatus.PUT,
                         self.sync_s3.upload_object(
                             {'name': 'key',
                              'storage_policy_index': 0,
                              'created_at': str(1e9)}, mock_ic))

    def test_upload_not_found(self):
        '''Should return NOT_FOUND for objects we can't GET'''
        mock_ic = mock.Mock(
            get_object_metadata=mock.Mock(
                side_effect=UnexpectedResponse('404 Not Found', None)))
        self.mock_boto3_client.head_object.side_effect = self.boto_not_found

        self.assertEqual(SyncS3.UploadStatus.NOT_FOUND,
                         self.sync_s3.upload_object(
                             {'name': 'key',
                              'storage_policy_index': 0,
                              'created_at': str(1e9)}, mock_ic))

    def test_upload_invalid_slo(self):
        '''Should return INVALID_SLO for SLOs we cannot upload'''
        object_meta = {'x-static-large-object': 'True',
                       'x-timestamp': 1e9}
        bad_manifest = [{'name': '/container/object'}]
        mock_ic = mock.Mock(
            get_object_metadata=mock.Mock(return_value=object_meta),
            get_object=mock.Mock(
                return_value=(200, object_meta,
                              FakeStream(content=json.dumps(bad_manifest)))))
        self.assertEqual(SyncS3.UploadStatus.INVALID_SLO,
                         self.sync_s3.upload_object(
                             {'name': 'key',
                              'storage_policy_index': 0,
                              'created_at': str(1e9)}, mock_ic))
        self.assertEqual(
            [mock.call(
                'SLO segment /container/object must include size and etag'),
             mock.call(
                'Failed to validate the SLO manifest for '
                'account/container/key')],
            self.logger.error.mock_calls)
        self.logger.error.reset_mock()
