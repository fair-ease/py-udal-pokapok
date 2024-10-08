from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import re
import requests
import xarray

from ..broker import Broker
from ..cache import Directory
from ..config import Config
from ..namedqueries import NamedQueryInfo, QueryName, QUERY_NAMES, QUERY_REGISTRY
from ..result import Result

from .data import cat_datasets
from .types import FloatMode, FloatType


localBrokerQueryNames: list[QueryName] = [
    'urn:pokapok:udal:argo:meta',
    'urn:pokapok:udal:argo:data',
]


localBrokerQueries: dict[QueryName, NamedQueryInfo] = \
    { k: v for k, v in QUERY_REGISTRY.items() if k in localBrokerQueryNames }


ARGO_URLS = [
    'https://data-argo.ifremer.fr',
    'https://usgodae.org/pub/outgoing/argo',
]


def _re_enum_options(enum) -> str:
    def value(e):
        if type(e) == str:
            return e
        else:
            return e.value
    values = [value(e) for e in enum]
    if any(map(lambda v: v == '', values)):
        option = '?'
    else:
        option = ''
    return '[' + ''.join(values) + f']{option}'


class ArgoBroker(Broker):

    _url: str
    _config: Config

    _query_names: list[QueryName] = localBrokerQueryNames

    _queries: dict[QueryName, NamedQueryInfo] = localBrokerQueries

    def __init__(self, url: str, config: Config):
        if url not in ARGO_URLS:
            raise Exception('Unsupported Argo URL')
        self._url = url
        self._config = config

    @property
    def queryNames(self) -> list[str]:
        return list(ArgoBroker._query_names)

    @property
    def queries(self) -> list[NamedQueryInfo]:
        return list(ArgoBroker._queries.values())

    @staticmethod
    def _argo_float_mode_type_re(float_mode: FloatMode|None, float_type: FloatType|list[FloatType]|None) -> str:
        # mode
        if float_mode == None:
            mode = _re_enum_options(FloatMode)
        else:
            mode = float_mode.value
        # type
        if float_type == None:
            ftype = _re_enum_options(FloatType)
        elif type(float_type) == FloatType:
            ftype = float_type.value
        elif type(float_type) == list or type(float_type) == list[FloatType]:
            if len(float_type) == 1:
                ftype = float_type[0].value
            else:
                ftype = _re_enum_options(map(lambda ft: ft.value, float_type))
        else:
            raise Exception(f'invalid float type {type(float_type)}')
        return f'{ftype}{mode}'

    def _argo_float_url(self, dac: str, float: str):
        return f'{self._url}/dac/{dac}/{float}/'

    def _argo_float_profiles_url(self, dac: str, float: str):
        return f'{self._url}/dac/{dac}/{float}/profiles/'

    def _argo_file_name_re(self,
            float_mode: FloatMode | None,
            float_type: FloatType | list[FloatType] | None,
            include_descending_cycles: bool
            ):
        mt = ArgoBroker._argo_float_mode_type_re(float_mode, float_type)
        if include_descending_cycles:
            d = 'D?'
        else:
            d = ''
        return f'.*/{mt}([0-9]*)_([0-9]*){d}\\.nc$'

    def _web_file_urls(self, url: str) -> list[str]:
        # TODO Error handling.
        page = requests.get(url)
        soup = BeautifulSoup(page.content, 'html.parser')
        links = soup.find_all('a')
        links = filter(lambda l: l['href'] == l.text and not l.text.endswith('/'), links)
        links = map(lambda l: urljoin(url, l.text), links)
        return list(links)

    def _meta_file_urls(self, dac: str, float: str) -> list[str]:
        return [self._argo_float_url(dac, float) + f'{float}_meta.nc']

    def _file_urls(self, dac: str, float: str) -> list[str]:
        # TODO Error handling.
        return self._web_file_urls(self._argo_float_profiles_url(dac, float))

    def _filter_argo_float_files(self, float_mode, float_type, descending_cycles, float_files: list[str]) -> list[str]:
        file_re = re.compile(self._argo_file_name_re(float_mode, float_type, descending_cycles))
        argo_files = []
        for f in float_files:
            if file_re.match(f) is not None:
                argo_files.append(f)
        return argo_files

    def _execute_argo_meta(self, params: dict[str, Any]):
        dac = params.get('dac')
        if dac == None:
            raise Exception('missing dac argument')
        float = params.get('float')
        if float == None:
            raise Exception('missing float argument')
        [url] = self._meta_file_urls(dac, float)
        result = None
        with Directory(self._config.cache_dir) as dir:
            meta_path = Path('argo', 'dac', dac, float)
            f = str(dir.download(url, meta_path, mkdir=True))
            meta = xarray.open_dataset(f)
            result = {
                'institution': meta.attrs.get('institution'),
                'title': meta.attrs.get('title'),
                'source': meta.attrs.get('source'),
                'references': meta.attrs.get('references'),
                'dimensions': list(meta.sizes.keys()),
                'variables': list(meta.variables.keys()),
            }
        return result

    def _execute_argo_data(self, params: dict[str, Any]):
        dac = params.get('dac')
        if dac == None:
            raise Exception('missing dac argument')
        float_mode = params.get('float_mode')
        float_type = params.get('float_type')
        float = params.get('float')
        if float == None:
            raise Exception('missing float argument')
        descending_cycles = params.get('descending_cycles')
        if descending_cycles == None:
            descending_cycles = True
        argo_file_urls = self._filter_argo_float_files(float_mode, float_type, descending_cycles, self._file_urls(dac, float))
        meta_file_urls = self._meta_file_urls(dac, float)
        all_files = []
        meta_path = Path('argo', 'dac', dac, float)
        profile_path = Path('argo', 'dac', dac, float, 'profiles')
        with Directory(self._config.cache_dir) as dir:
            for url in argo_file_urls:
                all_files.append(str(dir.download(url, profile_path, mkdir=True)))
            for url in meta_file_urls:
                all_files.append(str(dir.download(url, meta_path, mkdir=True)))
        results = cat_datasets([all_files])
        return results

    def execute(self, qn: QueryName, params: dict[str, Any] | None = None) -> Result:
        query = ArgoBroker._queries[qn]
        queryParams = params or {}
        match qn:
            case 'urn:pokapok:udal:argo:meta':
                return Result(query, self._execute_argo_meta(queryParams))
            case 'urn:pokapok:udal:argo:data':
                return Result(query, self._execute_argo_data(queryParams))
            case _:
                if qn in QUERY_NAMES:
                    raise Exception(f'unsupported query name "{qn}"')
                else:
                    raise Exception(f'unknown query name "{qn}"')
