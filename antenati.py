#!/usr/bin/env python3
"""
antenati.py: a tool to download data from the Portale Antenati
"""

__author__ = 'Giovanni Cerretani'
__copyright__ = 'Copyright (c) 2022, Giovanni Cerretani'
__license__ = 'MIT License'
__version__ = '3.1'
__contact__ = 'https://gcerretani.github.io/antenati/'

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from email.message import Message
from json import loads
from mimetypes import guess_extension
from os import chdir, mkdir, path
from pathlib import Path
from re import findall, search
from typing import Any, Optional

from certifi import where
from urllib3 import HTTPHeaderDict, HTTPSConnectionPool, PoolManager, make_headers
from click import echo, confirm
from slugify import slugify
from humanize import naturalsize
from tqdm import tqdm
import csv


@dataclass
class ProgressBar:
    """Progress Bar callbacks"""
    set_total: Callable[[int], None]
    update: Callable[[], None]


DEFAULT_N_THREADS: int = 8
DEFAULT_N_CONNECTIONS: int = 4


class AntenatiDownloader:
    """Downloader class"""

    url: str
    archive_id: str
    manifest: dict[str, Any]
    canvases: list[dict[str, Any]]
    dirname: Path
    gallery_length: int

    def __init__(self, url: str, first: int, last: int|None):
        self.url = url
        self.archive_id = self.__get_archive_id()
        self.manifest = self.__get_iiif_manifest()
        self.canvases = self.manifest['sequences'][0]['canvases'][first:last]
        self.dirname = self.__generate_dirname()
        self.gallery_length = len(self.canvases)

    @staticmethod
    def __http_headers() -> dict[str, Any]:
        """Generate HTTP headers to improve speed and to behave as a browser"""
        # Default headers to reduce data transfers
        headers = make_headers(
            keep_alive=True,
            accept_encoding=True
        )
        # SAN server return 403 if HTTP headers are not properly set.
        # - User-Agent: not required, but was required in the past
        # - Referer: required
        # - Origin: not required
        headers['Referer'] = 'https://antenati.cultura.gov.it/'
        return headers

    def __get_archive_id(self) -> str:
        """Get numeric archive ID from the URL"""
        archive_id_pattern = findall(r'(\d+)', self.url)
        if len(archive_id_pattern) < 2:
            raise RuntimeError(f'Cannot get archive ID from {self.url}')
        return archive_id_pattern[1]

    @staticmethod
    def __get_content_type(headers: HTTPHeaderDict):
        """Decode Content-Type header using email module."""
        msg = Message()
        msg['Content-Type'] = headers['Content-Type']
        return msg.get_content_type()

    @staticmethod
    def __get_content_charset(headers: HTTPHeaderDict):
        """Decode Content-Type header using email module."""
        msg = Message()
        msg['Content-Type'] = headers['Content-Type']
        return msg.get_content_charset()

    def __get_iiif_manifest(self) -> dict[str, Any]:
        """Get IIIF manifest as JSON from Portale Antenati gallery page"""
        pool = PoolManager(
            headers=self.__http_headers(),
            cert_reqs='CERT_REQUIRED',
            ca_certs=where()
        )
        http_reply = pool.request('GET', self.url)
        if http_reply.status != 200:
            raise RuntimeError(f'{self.url}: HTTP error {http_reply.status}')
        charset = self.__get_content_charset(http_reply.headers)
        html_content = http_reply.data.decode(charset).splitlines()
        manifest_line = next((line for line in html_content if 'manifestId' in line), None)
        if not manifest_line:
            raise RuntimeError(f'No IIIF manifest found at {self.url}')
        manifest_url_pattern = search(r'\'([A-Za-z0-9.:/-]*)\'', manifest_line)
        if not manifest_url_pattern:
            raise RuntimeError(f'Invalid IIIF manifest line found at {self.url}')
        manifest_url = manifest_url_pattern.group(1)
        http_reply = pool.request('GET', manifest_url)
        if http_reply.status != 200:
            raise RuntimeError(f'{self.url}: HTTP error {http_reply.status}')
        charset = self.__get_content_charset(http_reply.headers)
        return loads(http_reply.data.decode(charset))

    def __get_metadata_content(self, label: str) -> str:
        """Get metadata content of IIIF manifest given its label"""
        try:
            return next((i['value'] for i in self.manifest['metadata'] if i['label'] == label))
        except StopIteration as exc:
            raise RuntimeError(f'Cannot get {label} from manifest') from exc

    def __generate_dirname(self) -> Path:
        """Generate directory name from info in IIIF manifest"""
        context = self.__get_metadata_content('Contesto archivistico')
        year = self.__get_metadata_content('Titolo')
        typology = self.__get_metadata_content('Tipologia')
        return Path(slugify(f'{context}-{year}-{typology}-{self.archive_id}'))

    def print_gallery_info(self) -> None:
        """Print IIIF gallery info"""
        for i in self.manifest['metadata']:
            label = i['label']
            value = i['value']
            print(f'{label:<25}{value}')
        print(f'{self.gallery_length} images found.')

    def gallery_info(self) -> dict:
        """Return a dictionary of IIIF gallery info"""
        return {
                    'url': self.url,
                    'subtitles': self.__get_metadata_content('Titolo'),
                    'category': self.__get_metadata_content('Tipologia'),
                    'director': self.__get_metadata_content('Datazione'),
                    'comments': self.__get_metadata_content('Contesto archivistico'),
                    'actors': self.__get_metadata_content('Conservato da')
                }

    def check_dir(self, dirname: Optional[str] = None, interactive = True) -> None:
        """Check if directory already exists and chdir to it"""
        if dirname is not None:
            self.dirname = Path(dirname) / self.dirname
        print(f'Output directory: {self.dirname}')
        if path.exists(self.dirname):
            msg = f'Directory {self.dirname} already exists.'
            if not interactive:
                raise RuntimeError(msg)
            echo(msg)
            confirm('Do you want to proceed?', abort=True)
        else:
            mkdir(self.dirname)
        chdir(self.dirname)

    @staticmethod
    def __thread_main(pool: HTTPSConnectionPool, canvas: dict[str, Any]) -> int:
        """Main function for each thread"""
        url = canvas['images'][0]['resource']['@id']
        http_reply = pool.request('GET', url)
        if http_reply.status != 200:
            raise RuntimeError(f'{url}: HTTP error {http_reply.status}')
        content_type = AntenatiDownloader.__get_content_type(http_reply.headers)
        extension = guess_extension(content_type)
        if not extension:
            raise RuntimeError(f'{url}: Unable to guess extension "{content_type}"')
        label = slugify(canvas['label'])
        filename = f'{label}{extension}'
        with open(filename, 'wb') as img_file:
            img_file.write(http_reply.data)
        http_reply_size = len(http_reply.data)
        return http_reply_size

    @staticmethod
    def __executor(max_workers: int) -> ThreadPoolExecutor:
        """Create ThreadPoolExecutor with max_workers threads"""
        return ThreadPoolExecutor(max_workers=max_workers)

    @staticmethod
    def __pool(maxsize: int) -> HTTPSConnectionPool:
        """Create HTTPSConnectionPool with maxsize connections"""
        return HTTPSConnectionPool(
            host='iiif-antenati.cultura.gov.it',
            maxsize=maxsize,
            block=True,
            headers=AntenatiDownloader.__http_headers(),
            cert_reqs='CERT_REQUIRED',
            ca_certs=where()
        )

    def run_cli(self, n_workers: int, n_connections) -> int:
        """Main function spanning run function in a thread pool, with tqdm progress bar"""
        with tqdm(unit='img') as progress:
            progress_bar = ProgressBar(progress.reset, progress.update)  # type: ignore
            return self.run(n_workers, n_connections, progress_bar)

    def run(self, n_workers: int, n_connections: int, progress: ProgressBar) -> int:
        """Main function spanning run function in a thread pool"""
        with self.__executor(n_workers) as executor, self.__pool(n_connections) as pool:
            future_img = {executor.submit(self.__thread_main, pool, i): i for i in self.canvases}
            progress.set_total(self.gallery_length)
            gallery_size = 0
            for future in as_completed(future_img):
                progress.update()
                size = future.result()
                gallery_size += size
            return gallery_size
        
    @staticmethod
    def save_csv_file(filename: str, header: list, body: list[list]) -> None:
        """Save a csv file with info about gallery and pages"""
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile, delimiter=';')
            writer.writerow(header)
            for r in body:
                writer.writerow(r)

def main() -> None:
    """Main"""

    # Parse arguments
    parser = ArgumentParser(
        description=__doc__,
        epilog=__copyright__,
        formatter_class=ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('url', metavar='URL', type=str, help='url of the gallery page')
    parser.add_argument('-n', '--nthreads', type=int, help='max n. of threads', default=DEFAULT_N_THREADS)
    parser.add_argument('-c', '--nconn', type=int, help='max n. of connections', default=DEFAULT_N_CONNECTIONS)
    parser.add_argument('-f', '--first', type=int, help='first image to download', default=0)
    parser.add_argument('-l', '--last', type=int, help='first image NOT to download', default=None)
    parser.add_argument('-v', '--version', action='version', version=__version__)
    args = parser.parse_args()

    # Initialize
    downloader = AntenatiDownloader(args.url, args.first, args.last)

    # Print gallery info
    downloader.print_gallery_info()

    # Check if directory already exists and chdir to it
    downloader.check_dir()

    # Save a csv file w/ info about the gallery and every downloaded page
    p1 = list(downloader.gallery_info().values())
    body2 = [[slugify(x['label']), x['images'][0]['resource']['@id']] for x in downloader.canvases]

    AntenatiDownloader.save_csv_file(
        filename = 'info.csv',
        header = list(downloader.gallery_info().keys()) + ['mediatype','Languages'],
        body = [p1 + p2 for p2 in body2]
    )

    # Run
    gallery_size = downloader.run_cli(args.nthreads, args.nconn)

    # Print summary
    print(f'Done. Total size: {naturalsize(gallery_size)}')



if __name__ == '__main__':
    main()
