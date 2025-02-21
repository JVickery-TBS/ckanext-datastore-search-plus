import pysolr
import json
import requests

from typing import Any, Optional, Dict
from ckan.types import Context, DataDict

from ckan.plugins.toolkit import _, config, get_action

from ckanext.datastore_search.backend import (
    DatastoreSearchBackend,
    DatastoreSearchException
)

MAX_ERR_LEN = 1000

from pprint import pprint
from logging import getLogger
log = getLogger(__name__)


class DatastoreSolrBackend(DatastoreSearchBackend):
    """
    SOLR class for datastore search backend.
    """
    timeout = config.get('solr_timeout')
    default_solr_fields = ['_id', '_version_', 'indexed_ts']

    @property
    def field_type_map(self):
        """
        Map of DataStore field types to their corresponding
        SOLR field types.

        NOTE: These are all based off of postgres data types.
              This is mainly to support the extending of DataStore
              types. e.g. through TableDesigner interfaces.
        """
        return {
            # numeric types
            'smallint': 'int',
            'integer': 'int',
            'bigint': 'int',
            'decimal': 'float',
            'numeric': 'float',
            'real': 'double',
            'double precision': 'double',
            'smallserial': 'int',
            'serial': 'int',
            'bigserial': 'int',
            # monetary types
            'money': 'float',
            # char types
            'character varying': 'text',
            'varchar': 'text',
            'character': 'text',
            'char': 'text',
            'bpchar': 'text',
            'text': 'text',
            # binary types
            'bytea': 'binary',
            # datetime types
            'timestamp': 'date',
            'date': 'date',
            'time': 'date',
            'interval': 'date',
            # bool types
            'boolean': 'boolean',
            # TODO: map geometric types
            # TODO: map object/array types
        }

    def _make_connection(self, core_name: str) -> Optional[pysolr.Solr]:
        """
        Tries to make a SOLR connection to a core.
        """
        conn_string = f'{self.url}/solr/{core_name}'
        conn = pysolr.Solr(conn_string, timeout=self.timeout)
        try:
            resp = json.loads(conn.ping())
            if resp.get('status') == 'OK':
                return conn
        except pysolr.SolrError:
            pass

    def _send_api_request(self,
                          method: str,
                          endpoint: str,
                          body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Sends a SOLR API v2 request.

        NOTE: pysolr does not have an API v2 interface.
        """
        conn_string = f'{self.url}/api/{endpoint}'
        if method == 'POST':
            resp = requests.post(
                conn_string,
                headers={'Content-Type': 'application/json'},
                timeout=self.timeout,
                data=json.dumps(body) if body else None)
        else:
            resp = requests.get(conn_string,
                                timeout=self.timeout)
        return resp.json()

    def create(self,
               context: Context,
               data_dict: DataDict,
               connection: Optional[pysolr.Solr] = None) -> Any:
        """
        Create or update & reload/reindex a core if the fields have changed.
        """
        rid = data_dict.get('resource_id')
        core_name = f'{self.prefix}{rid}'
        conn = self._make_connection(core_name=core_name) if not connection else connection
        if not conn:
            errmsg = _('Could not create SOLR core %s') % core_name
            req_body = {'create': [{'name': core_name,
                                    'configSet': 'datastore_resource'}]}
            resp = self._send_api_request(method='POST',
                                            endpoint='cores',
                                            body=req_body)
            if 'error' in resp:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug')
                    else resp['error'].get('msg', errmsg)[:MAX_ERR_LEN])
            conn = self._make_connection(core_name=core_name)
        if not conn:
            raise DatastoreSearchException(
                _('Could not connect to SOLR core %s') % core_name)

        try:
            solr_fields = json.loads(conn._send_request(
                method='GET', path='schema/fields'))['fields']
        except pysolr.SolrError as e:
            raise DatastoreSearchException(
                errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
        keyed_solr_fields = {}
        for solr_field in solr_fields:
            if solr_field['name'] in self.default_solr_fields:
                continue
            keyed_solr_fields[solr_field['name']] = solr_field
        ds_field_ids = []
        new_fields = []
        updated_fields = []
        remove_fields = []
        #TODO: check for unique keys instead of just _id
        for ds_field in data_dict.get('fields', []):
            if ds_field['id'] not in self.default_solr_fields:
                ds_field_ids.append(ds_field['id'])
            if ds_field['id'] not in keyed_solr_fields:
                new_fields.append({
                    'name': ds_field['id'],
                    'type': self.field_type_map[ds_field['type']],
                    'stored': True,
                    'indexed': True})
                continue
            if self.field_type_map[ds_field['type']] == keyed_solr_fields[ds_field['id']]['type']:
                continue
            updated_fields.append(dict(keyed_solr_fields[ds_field['id']],
                                       type=self.field_type_map[ds_field['type']]))
        for field_name in [i for i in keyed_solr_fields.keys() if i not in ds_field_ids]:
            remove_fields.append({'name': field_name})

        for f in new_fields:
            errmsg = _('Could not add field %s to SOLR Schema %s' %
                       (f['name'], core_name))
            try:
                resp = json.loads(conn._send_request(
                    method='POST', path='schema',
                    body=json.dumps({'add-field': f}),
                    headers={'Content-Type': 'application/json'}))
            except pysolr.SolrError as e:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
            if 'error' in resp:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug')
                    else resp['error'].get('msg', errmsg)[:MAX_ERR_LEN])

        for f in updated_fields:
            errmsg = _('Could not update field %s on SOLR Schema %s' %
                       (f['name'], core_name))
            try:
                resp = json.loads(conn._send_request(
                    method='POST', path='schema',
                    body=json.dumps({'replace-field': f}),
                    headers={'Content-Type': 'application/json'}))
            except pysolr.SolrError as e:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
            if 'error' in resp:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug')
                    else resp['error'].get('msg', errmsg)[:MAX_ERR_LEN])

        for f in remove_fields:
            errmsg = _('Could not delete field %s from SOLR Schema %s' %
                       (f['name'], core_name))
            try:
                resp = json.loads(conn._send_request(
                    method='POST', path='schema',
                    body=json.dumps({'delete-field': f}),
                    headers={'Content-Type': 'application/json'}))
            except pysolr.SolrError as e:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
            if 'error' in resp:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug')
                    else resp['error'].get('msg', errmsg)[:MAX_ERR_LEN])

        #TODO: datastore_create can take records[] as well...
        #TODO: if the method == 'insert', then _id is not included...what to do then...

        if new_fields or updated_fields or remove_fields:
            #TODO: reindex as something has changed...
            return

        #TODO: check if ds totalRecords is not same as indexed records...

    def upsert(self,
               context: Context,
               data_dict: DataDict,
               connection: Optional[pysolr.Solr] = None) -> Any:
        """
        Insert records into the SOLR index.
        """
        rid = data_dict.get('resource_id')
        core_name = f'{self.prefix}{rid}'
        conn = self._make_connection(core_name=core_name) if not connection else connection

        if not conn:
            ds = get_action('datastore_search')(context, {'resource_id': rid,
                                                          'limit': 0})
            create_dict = {
                'resource_id': rid,
                'fields': [f for f in ds['fields'] if
                           f['id'] not in self.default_solr_fields]}
            self.create(context, create_dict)
            conn = self._make_connection(core_name=core_name)
        if not conn:
            errmsg = _('Failed to index records for %s' % core_name)
            raise DatastoreSearchException(errmsg)

        #TODO: if the method == 'insert', then _id is not included...what to do then...

        if data_dict['records']:
            for r in data_dict['records']:
                try:
                    conn.add(docs=[r], commit=False)
                except pysolr.SolrError as e:
                    errmsg = _('Failed to index records for %s' % core_name)
                    raise DatastoreSearchException(
                        errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
            conn.commit(waitSearcher=False)

        #TODO: check if ds totalRecords is not same as indexed records...

    def search(self,
               context: Context,
               data_dict: DataDict,
               connection: Optional[pysolr.Solr] = None) -> Any:
        """
        Searches the SOLR records.
        """
        rid = data_dict.get('resource_id')
        core_name = f'{self.prefix}{rid}'
        conn = self._make_connection(core_name=core_name) if not connection else connection

        log.info('    ')
        log.info('DEBUGGING::')
        log.info('    ')
        log.info(pprint(data_dict))
        log.info('    ')

    def delete(self,
               context: Context,
               data_dict: DataDict,
               connection: Optional[pysolr.Solr] = None) -> Any:
        """
        Removes records from the SOLR index, or deletes the core entirely.
        """
        rid = data_dict.get('resource_id')
        core_name = f'{self.prefix}{rid}'
        conn = self._make_connection(core_name=core_name) if not connection else connection

        if not conn:
            return

        if not data_dict.get('filters'):
            errmsg = _('Could not delete SOLR core %s') % core_name
            try:
                conn.delete(q='*:*', commit=False)
            except pysolr.SolrError as e:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
            conn.commit(waitSearcher=False)
            resp = self._send_api_request(method='POST',
                                          endpoint=f'cores/{core_name}/unload')
            if 'error' in resp:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug')
                    else resp['error'].get('msg', errmsg)[:MAX_ERR_LEN])
            return

        for key, value in data_dict.get('filters', {}).items():
            errmsg = _('Could not delete records %s,%s in SOLR core %s') % (key,
                                                                            value,
                                                                            core_name)
            try:
                conn.delete(q='%s:%s' % (key, value), commit=False)
            except pysolr.SolrError as e:
                raise DatastoreSearchException(
                    errmsg if not config.get('debug') else e.args[0][:MAX_ERR_LEN])
