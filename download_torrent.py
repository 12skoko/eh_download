import os.path
import time
import requests
import re
import random

import config
import datetime
import qbittorrentapi
import argparse
from sqlalchemy import create_engine, select, update, desc
from sqlalchemy.orm import sessionmaker
from model import Manga


class SqlManager():
    def __init__(self, run_mode):
        self.engine = create_engine(config.sql_engine)
        self.SqlSession = sessionmaker(bind=self.engine)
        self.run_mode = run_mode

    def torrent_category(self):
        if self.run_mode == "main":
            torrent_category = 'autoehentai'
        elif self.run_mode == "old":
            torrent_category = 'ehentai'
        elif self.run_mode == "special":
            torrent_category = 'specialehentai'
        else:
            raise ValueError(f"Unknown run_mode: {self.run_mode}")
        return torrent_category

    def select_download_torrent(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = (sql_session.query(Manga)
                         .filter(Manga.autostate == 2)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "old":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 2)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "special":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 13)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def no_seeds(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 6
            elif self.run_mode == "old":
                manga.state = 6
            elif self.run_mode == "special":
                manga.state = 15
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def rollback(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 2
            elif self.run_mode == "old":
                manga.state = 2
            elif self.run_mode == "special":
                manga.state = 13
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def add_torrent_error(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = -3
            elif self.run_mode == "old":
                manga.state = -3
            elif self.run_mode == "special":
                manga.state = -3
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def add_torrent_success(self, filename, torrenthash, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 4
            elif self.run_mode == "old":
                manga.state = 5
            elif self.run_mode == "special":
                manga.state = 14
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.filename = filename
            manga.torrenthash = torrenthash

            sql_session.commit()

    def manga_unavailable(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = -2
            elif self.run_mode == "old":
                manga.state = -2
            elif self.run_mode == "special":
                manga.state = -2
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def video_exists(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = -6
            elif self.run_mode == "old":
                manga.state = -6
            elif self.run_mode == "special":
                manga.state = -6
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()


def get_torrent_link(url):
    for dev in range(5):
        proxy = config.proxy_pool[(datetime.datetime.now().hour + dev) % len(config.proxy_pool)]
        try:
            data = requests.get(url, headers=config.header, cookies=config.cookies_non_donation, proxies=proxy).text
            torrent_exist = re.search("There are no torrents for this gallery", data)
            if torrent_exist is not None:
                return None
            manga_unavailable = re.search("This gallery is currently unavailable", data)
            if manga_unavailable is not None:
                return -1
            if 'torrent' not in data:
                raise ValueError(f"get torrent link error: {url}")
            return data

        except:
            print('get torrent link error:', url, proxy)
            print("repeat:")
            time.sleep(2)

    raise ValueError(f"get torrent link error: {url}")


def download_torrent():
    qbt_client = qbittorrentapi.Client(**config.qbit_login)
    qbt_client.auth_log_in()

    mangaList = sql_manager.select_download_torrent()
    lense = len(mangaList)
    dev = 0
    i = 0
    errorflag2 = 0
    errortemplist = []
    while i < lense:
        proxy = config.proxy_pool[(datetime.datetime.now().hour + dev) % len(config.proxy_pool)]
        manga = mangaList[i]
        print(str(i + 1) + '/' + str(lense))
        if manga.torrentlink == '':
            sql_manager.no_seeds(manga.manga_id)
            i += 1
            continue
        url = manga.torrentlink

        data = get_torrent_link(url)

        if data == -1:
            sql_manager.manga_unavailable(manga.manga_id)

        else:
            torrentLink = ''
            if data:
                seeds = 0
                size = ''
                re_torrent = r"""(?s)Posted:</span> <span>(.*?)</span></td>.*?Size:</span> (.*?)</td>.*?Seeds:</span> (\d+)</td>.*?Peers:</span> (\d+)</td>.*?Downloads:</span> (\d+)</td>.*?<a href=\"(.*?)\" onclick=\"document\.location='(.*?)'; return false\">(.*?)</a></td>"""
                torrentList = re.findall(re_torrent, data)
                exclude_resolutions = ["1280x", "800x", "1920x", "2560x"]

                video_mark = ["mp4", "video"]
                video_extensions_flag = 0

                skip_video_check = False
                if "skip video" in manga.remark.lower():
                    skip_video_check = True

                for torrent in torrentList:
                    if not skip_video_check and any(ext in torrent[7].lower() for ext in video_mark):
                        video_extensions_flag = 1
                        break
                    if any(res in torrent[7] for res in exclude_resolutions):
                        continue
                    if int(torrent[2]) > 0:
                        torrentLink = torrent[5]
                        size = torrent[1]
                        seeds = int(torrent[2])
                        break
                for torrent in torrentList:
                    if any(res in torrent[7] for res in exclude_resolutions):
                        continue
                    if int(torrent[2]) == 0:
                        continue
                    if int(torrent[2]) > seeds and size == torrent[1]:
                        seeds = int(torrent[2])
                        torrentLink = torrent[5]

                if video_extensions_flag == 1:
                    sql_manager.video_exists(manga.manga_id)
                    print("Maybe video exists:", manga.manga_id)
                    i += 1
                    if i < lense:
                        time.sleep(30 + random.randint(0, 60))
                    continue

                # print(torrentLink)

            if torrentLink == '':
                sql_manager.no_seeds(manga.manga_id)

            else:
                time.sleep(1)
                try:
                    response = requests.get(torrentLink, headers=config.header, cookies=config.cookies_non_donation, proxies=proxy)
                    if response.text == 'The torrent file could not be found. Most likely you have mistyped the URL, or the torrent is no longer available.':
                        raise 'error'
                except:
                    time.sleep(1)
                    dev += 1
                    print('error', torrentLink)
                    proxy = config.proxy_pool[(datetime.datetime.now().hour + dev) % len(config.proxy_pool)]
                    response = requests.get(torrentLink, headers=config.header, cookies=config.cookies_non_donation, proxies=proxy)

                if response.text == 'The torrent file could not be found. Most likely you have mistyped the URL, or the torrent is no longer available.':
                    errorflag2 += 1
                    print('The torrent file could not be found')
                    sql_manager.no_seeds(manga.manga_id)
                    errortemplist.append(manga)
                    if errorflag2 >= 5:
                        for mangatemp in errortemplist:
                            sql_manager.rollback(mangatemp.manga_id)
                        raise 'download torrent error:The torrent file could not be found'

                else:
                    errorflag2 = 0
                    errortemplist = []

                    print(torrentLink)
                    idnum = manga.manga_id.split('/')[0]
                    qbt_client.torrents_add(torrent_files=response.content,
                                            save_path=config.qbit_torrent_path + idnum, category=sql_manager.torrent_category(),
                                            rename=idnum)
                    time.sleep(3)
                    torrentinfo = ''
                    torrents_qbit = qbt_client.torrents.info()
                    for torrent in torrents_qbit:
                        if torrent.name == idnum:
                            torrentinfo = torrent
                            break
                    if torrentinfo == '':
                        time.sleep(3)
                        torrents_qbit = qbt_client.torrents.info()
                        for torrent in torrents_qbit:
                            if torrent.name == idnum:
                                torrentinfo = torrent
                                break
                    if torrentinfo == '':
                        print('torrent add error', idnum)
                        sql_manager.add_torrent_error(manga.manga_id)
                    else:
                        filename = torrentinfo.content_path[len(torrentinfo.save_path) + 1:]
                        print(filename)
                        sql_manager.add_torrent_success(filename, torrentinfo.hash, manga.manga_id)

        print(manga.manga_id)
        i += 1
        if i < lense:
            time.sleep(30 + random.randint(0, 60))

    print('done')


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--main", action="store_true")
    parser.add_argument("--old", action="store_true")
    parser.add_argument("--special", action="store_true")

    args = parser.parse_args()

    if args.special == True:
        run_mode = "special"
    elif args.old == True:
        run_mode = "old"
    elif args.main == True:
        run_mode = "main"
    else:
        run_mode = "old"

    print(run_mode)

    sql_manager = SqlManager(run_mode)

    download_torrent()
