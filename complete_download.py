import argparse
import qbittorrentapi
import os
import zipfile
import requests
import time
import paramiko
import re
from bs4 import BeautifulSoup
import random
import config
import datetime
import html
from requests_toolbelt.multipart.encoder import MultipartEncoder
import shutil
import hashlib
import json
import aria2p
from sqlalchemy import create_engine, select, update, desc, and_, or_
from sqlalchemy.orm import sessionmaker
from model import Manga, MangaInfo, EhTagTranslation
import ehentai_utils


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
            raise "unkown run_mode"
        return torrent_category

    def complete_torrent_success(self, torrenthash, is_dir=None):
        with self.SqlSession() as sql_session:

            if self.run_mode == "main":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.autostate == 4
                )).first()  # type: ignore

                if not manga:
                    return

                manga.autostate = 5
            elif self.run_mode == "old":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.state == 5
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 7
            elif self.run_mode == "special":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.state == 14
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 7
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            if is_dir == "isdir":
                manga.remark = "isdir"

            sql_session.commit()

    def complete_torrent_fatel(self, torrenthash):
        with self.SqlSession() as sql_session:

            if self.run_mode == "main":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.autostate == 4
                )).first()  # type: ignore

                if not manga:
                    return

                manga.autostate = 6
            elif self.run_mode == "old":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.state == 5
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 6
            elif self.run_mode == "special":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.torrenthash == torrenthash,
                    Manga.state == 14
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 15
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def complete_hah_select(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(Manga.autostate == 7)  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(Manga.state == 9)  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(Manga.state == 9)  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def complete_hah_update(self, alias, manga_id):
        with self.SqlSession() as sql_session:

            if self.run_mode == "main":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.manga_id == manga_id,
                    Manga.autostate == 7
                )).first()  # type: ignore

                if not manga:
                    return

                manga.autostate = 9
            elif self.run_mode == "old":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.manga_id == manga_id,
                    Manga.state == 9
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 10
            elif self.run_mode == "special":
                manga = sql_session.query(Manga).filter(and_(
                    Manga.manga_id == manga_id,
                    Manga.state == 9
                )).first()  # type: ignore

                if not manga:
                    return

                manga.state = 10
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.alias = alias

            sql_session.commit()

    def compress_torrent_select(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(and_(Manga.autostate == 5, Manga.remark == "isdir"))  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(and_(Manga.state == 7, Manga.remark == "isdir"))  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(and_(Manga.state == 7, Manga.remark == "isdir"))  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def compress_error(self, remark, manga_id, compress_type):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = -4
            elif self.run_mode == "old":
                manga.state = -4
            elif self.run_mode == "special":
                manga.state = -4
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.remark = f"compress {compress_type} error | {remark}"

            sql_session.commit()

    def compress_hah_select(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(Manga.autostate == 9)  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(Manga.state == 10)  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(Manga.state == 10)  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def compress_hah_success(self, filename, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 10
            elif self.run_mode == "old":
                manga.state = 12
            elif self.run_mode == "special":
                manga.state = 12
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.filename = filename

            sql_session.commit()

    def collect_torrent_select(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = (sql_session.query(Manga)
                         .filter(Manga.autostate == 5)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "old":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 7)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "special":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 7)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def collect_torrent_success(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 8
            elif self.run_mode == "old":
                manga.state = 8
            elif self.run_mode == "special":
                manga.state = 8
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def uploadall_torrent(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(and_(Manga.autostate == 8, Manga.state == None))  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(Manga.state == 8)  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(Manga.state == 8)  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def uploadall_hah(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(and_(Manga.autostate == 10, Manga.state == None))  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(Manga.state == 12)  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(Manga.state == 12)  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def uploadall_direct(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = sql_session.query(Manga).filter(and_(Manga.autostate == 11, Manga.state == None))  # type: ignore
            elif self.run_mode == "old":
                query = sql_session.query(Manga).filter(Manga.state == 11)  # type: ignore
            elif self.run_mode == "special":
                query = sql_session.query(Manga).filter(Manga.state == 11)  # type: ignore
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def apiupload_error(self, errorlog, file_path, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = -5
            elif self.run_mode == "old":
                manga.state = -5
            elif self.run_mode == "special":
                manga.state = -5
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.remark = f"{errorlog} | {file_path}"

            sql_session.commit()

    def apiupload_success(self, arcid, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            manga.arcid = arcid
            manga.state = 0

            sql_session.commit()

    def delete_outdate_select(self):
        with self.SqlSession() as sql_session:
            query = sql_session.query(Manga).filter(
                and_(Manga.state == -1,
                     or_(Manga.remark == None,
                         Manga.remark != "deleted"
                         )
                     )
            )  # type: ignore
            return query.all()

    def delete_outdate_success(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)
            manga.remark = "deleted"
            sql_session.commit()

    def handle_conflicts_select(self):
        with self.SqlSession() as sql_session:
            query = sql_session.query(Manga).filter(Manga.autostate == 12)  # type: ignore
            return query.all()

    def handle_conflicts_success(self, filename, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)
            manga.filename = filename

            if self.run_mode == "main":
                manga.autostate = 8
            elif self.run_mode == "old":
                manga.state = 8
            elif self.run_mode == "special":
                manga.state = 8
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")

            sql_session.commit()

    def compress_torrent_success(self, filename, alias, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)
            manga.filename = filename
            manga.alias = alias
            manga.remark = 'compressed'
            sql_session.commit()

    def insert_manga_info(self, manga_info):
        with self.SqlSession() as sql_session:
            sql_session.merge(manga_info)
            sql_session.commit()

    def parent_outdate(self, parent):
        if parent != 'None':
            with self.SqlSession() as sql_session:
                parent_manga = sql_session.query(Manga).filter(Manga.manga_id.like(f"{parent}/%")).first()
                if parent_manga:
                    parent_manga.state = -1
                    sql_session.commit()
                    print(f"parent_outdate: {parent_manga.manga_id}")

    def get_mangainfo(self, manga_id):
        with self.SqlSession() as sql_session:
            mangainfo = sql_session.get(MangaInfo, manga_id)
            return mangainfo

    def is_need_to_delete_torrent(self, torrenthash):
        with self.SqlSession() as sql_session:
            manga = sql_session.query(Manga).filter(and_(
                Manga.torrenthash == torrenthash,
                or_(Manga.state == 0,
                    Manga.state == -1
                    )
            )).first()  # type: ignore
            if manga:
                return True
            else:
                return False

    def is_need_to_delete_file(self, idnum):
        with self.SqlSession() as sql_session:
            manga = sql_session.query(Manga).filter(and_(
                Manga.manga_id.like(f"{idnum}/%"),
                or_(Manga.state == 0,
                    Manga.state == -1
                    )
            )).first()  # type: ignore
            if manga:
                return True
            else:
                return False


def api_upload(manga, directorypath):
    print(manga.manga_id)
    mangainfo = sql_manager.get_mangainfo(manga.manga_id)

    file_path = os.path.join(directorypath, manga.filename)
    file_path = os.path.normpath(file_path).replace('\\', '/')
    print(file_path)
    size = os.path.getsize(file_path)
    if size > config.max_file_size:
        sql_manager.apiupload_error("文件过大", file_path, manga.manga_id)
        print("上传失败，文件过大，", manga.manga_id)
        return
    file_checksum = ehentai_utils.calculate_sha1(file_path)

    date_added = int(time.time())
    tagstr = f'romaname:{mangainfo.romaname},source:{mangainfo.link},category:{mangainfo.category},uploader:{mangainfo.uploader},postedtime:{mangainfo.postedtime},language:{mangainfo.language},pages:{mangainfo.pages},favorited:{mangainfo.favorited},ratingcount:{mangainfo.ratingcount},rating:{mangainfo.rating},updatetime:{mangainfo.fetchtime},date_added:{date_added}'
    tagstr = tagstr + ',' + mangainfo.tagtran + ',' + mangainfo.tag

    data = {
        'title': mangainfo.name,
        'tags': tagstr,
        'file_checksum': file_checksum,
    }

    with open(file_path, 'rb') as f:
        files = {'file': (file_path, f, "application/zip")}
        response = requests.put(config.raragi_url + '/api/archives/upload', data=data, files=files, headers=config.raragi_auth)

    if response.status_code == 200:
        arcid = response.json()['id']
        sql_manager.apiupload_success(arcid, manga.manga_id)
        print('Upload success')
    else:
        errorlog = str(response.status_code) + ' ' + response.text.replace('"', "'")
        print("上传失败，", manga.manga_id)
        print("状态码:", response.status_code, "错误信息:", response.text)
        if size > 6442450944:
            print("文件过大，上传失败，跳过记录")
        else:
            sql_manager.apiupload_error(errorlog, file_path, manga.manga_id)

def delete_log():
    print('-------------------delete_log-------------------')
    file_names = os.listdir(config.logpath)
    for file_name in file_names:
        file_path = os.path.join(config.logpath, file_name)
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
        if content in config.emptyLogList:
            os.remove(file_path)


def add_fatel():
    print('-------------------add_fatel-------------------')
    current_timestamp = int(time.time())
    for torrent in qbt_client.torrents_info():
        # print(torrent)
        if torrent.completion_on == 0 and current_timestamp - torrent.added_on > 604800:
            print(torrent)
            qbt_client.torrents_add_tags(tags='fatel', torrent_hashes=torrent.hash)


def delete_outdate():
    print('-------------------delete_outdate-------------------')
    manga_list = sql_manager.delete_outdate_select()
    for manga in manga_list:
        searchurl = config.raragi_url + '/api/search?filter=' + manga.manga_id
        res1 = json.loads(requests.get(searchurl, headers=config.raragi_auth).text)
        if res1["recordsFiltered"] == 1 and res1["data"] != []:
            print('deleted_outdate:', manga.manga_id)
            aid = res1['data'][0]['arcid']
            # print(id)
            deleteurl = config.raragi_url + '/api/archives/' + aid
            # print(deleteurl)
            res2 = requests.delete(deleteurl, headers=config.raragi_auth)
            # print(res2.text)
            res3 = json.loads(requests.get(searchurl, headers=config.raragi_auth).text)
            if res3['data'] == []:
                sql_manager.delete_outdate_success(manga.manga_id)

        elif res1["recordsFiltered"] > 1:
            print(searchurl)
            raise '过期存档id重复'

        elif res1["recordsFiltered"] == 0:
            sql_manager.delete_outdate_success(manga.manga_id)


def handle_conflicts():
    print('-------------------handle_conflicts-------------------')
    manga_list = sql_manager.handle_conflicts_select()
    for manga in manga_list:
        filename = '[' + manga.manga_id.split('/')[0] + ']' + manga.filename
        old_name = os.path.join(config.torrent_download_path, manga.manga_id.split('/')[0], manga.filename)
        new_name = os.path.join(config.torrent_download_path, manga.manga_id.split('/')[0], filename)
        os.rename(old_name, new_name)
        print('handleConflicts:', filename)
        sql_manager.handle_conflicts_success(filename, manga.manga_id)


def complete_torrent():
    print('-------------------complete_torrent-------------------')
    torrents = qbt_client.torrents_info()
    for torrent in torrents:
        if torrent.category == sql_manager.torrent_category():
            # print(torrent.name)
            if torrent['completion_on'] > 0:
                print('complete torrent:', torrent.name)
                if os.path.isdir(
                        os.path.join(config.torrent_download_path, torrent.content_path[len(config.qbit_torrent_path):])):
                    sql_manager.complete_torrent_success(torrent.hash, "isdir")
                else:
                    sql_manager.complete_torrent_success(torrent.hash)
            if torrent['tags'] == 'fatel':
                print('fatel:', torrent.name)
                sql_manager.complete_torrent_fatel(torrent.hash)


def complete_hah():
    print('-------------------complete_hah-------------------')
    mangalist = sql_manager.complete_hah_select()
    for manga in mangalist:
        print(manga.manga_id)
        partial_name = '[' + manga.manga_id.split('/')[0] + ']'
        flag = ehentai_utils.check_complete(config.hah_download_path, partial_name)
        if flag[0]:
            sql_manager.complete_hah_update(flag[1], manga.manga_id)


def compress_torrent():
    print('-------------------compress_torrent-------------------')
    manga_list = sql_manager.compress_torrent_select()
    i = 1
    lense = str(len(manga_list))
    for manga in manga_list:
        path = os.path.join(config.torrent_download_path, manga.manga_id.split('/')[0], manga.filename)
        print(str(i) + '/' + lense + ':', manga.manga_id)
        zip_file_name = '[' + manga.manga_id.split('/')[0] + ']' + re.sub(r'[\\/*?:"<>|]', '_', manga.filename) + '.zip'
        zip_file_path = os.path.join(config.torrent_zip_path, zip_file_name)
        try:
            ehentai_utils.create_zip_file(path, zip_file_path)
        except Exception as e:
            print('compress error:', manga.manga_id)
            sql_manager.compress_error(e, manga.manga_id, "torrent")
            i += 1
            continue
        sql_manager.compress_torrent_success(zip_file_name, manga.filename, manga.manga_id)
        i += 1


def compress_hah():
    print('-------------------compress_hah-------------------')
    manga_list = sql_manager.compress_hah_select()
    i = 1
    lense = str(len(manga_list))
    for manga in manga_list:
        partial_name = '[' + manga.manga_id.split('/')[0] + ']'
        folder_name = ehentai_utils.get_folder_name(config.hah_download_path, partial_name)
        print(str(i) + '/' + lense + ':', manga.manga_id)
        idname = '[' + manga.manga_id.split('/')[0] + ']'
        if idname in config.too_long_name_list:
            zip_file_name = config.too_long_name_list[idname]
        else:
            zip_file_name = idname + re.sub(r'[\\/*?:"<>|]', '_', manga.name) + '.zip'
        zip_file_path = os.path.join(config.hah_zip_path, zip_file_name)
        try:
            ehentai_utils.create_zip_file(folder_name, zip_file_path)
        except Exception as e:
            print(zip_file_name)
            print('compress error:', manga.manga_id, '\n', e)
            sql_manager.compress_error(e, manga.manga_id, "hah")
            i += 1
            continue
        sql_manager.compress_hah_success(zip_file_name, manga.manga_id)
        i += 1


def collect_torrent():
    print('-------------------collect_torrent-------------------')
    se = requests.session()
    tagTrans = EhTagTranslation()
    manga_list = sql_manager.collect_torrent_select()
    lense = len(manga_list)
    dev = 0
    i = 0
    errorflag = 0
    while i < lense:
        proxy = config.proxy_pool[(datetime.datetime.now().hour + dev) % len(config.proxy_pool)]
        manga = manga_list[i]
        print(str(i + 1) + '/' + str(lense))
        url = manga.link
        try:
            data = se.get(url, headers=config.header, cookies=config.cookies_non_donation, proxies=proxy).text
            soup = BeautifulSoup(data, 'lxml')
            manga_info, downloadlink, parent = ehentai_utils.parse_info(soup, tagTrans)
        except:
            print('requests error ', url, proxy)
            # print(data)
            errorflag += 1
            dev += 1
            if errorflag >= 5:
                raise ValueError(f"requests error {url} {proxy}")
            else:
                time.sleep(2)
                continue

        errorflag = 0
        manga_info.manga_id = manga.manga_id
        manga_info.state = 1
        manga_info.filename = manga.filename
        manga_info.link = url

        sql_manager.insert_manga_info(manga_info)
        print('insert mangainfo:', manga.manga_id)

        sql_manager.collect_torrent_success(manga.manga_id)

        if run_mode == "main":
            sql_manager.parent_outdate(parent)

        i += 1
        if i < lense:
            time.sleep(10 + random.randint(10, 30))


def upload_all():
    print('-------------------uploadall-------------------')

    manga_list = sql_manager.uploadall_torrent()
    length = str(len(manga_list))
    i = 1
    for manga in manga_list:
        print('torrent:' + str(i) + '/' + length)
        if manga.remark == 'compressed':
            api_upload(manga, config.torrent_zip_path)
        else:
            api_upload(manga, os.path.join(config.torrent_download_path, manga.manga_id.split('/')[0]))
        i += 1
        time.sleep(2)

    manga_list = sql_manager.uploadall_hah()
    length = str(len(manga_list))
    i = 1
    for manga in manga_list:
        print('hah:' + str(i) + '/' + length)
        api_upload(manga, config.hah_zip_path)
        i += 1
        time.sleep(2)

    manga_list = sql_manager.uploadall_direct()
    length = str(len(manga_list))
    i = 1
    for manga in manga_list:
        print('direct:' + str(i) + '/' + length)
        if use_aria2:
            api_upload(manga, config.aria2_download_path)
        else:
            api_upload(manga, config.direct_download_path)
        i += 1
        time.sleep(2)

    requests.post(config.raragi_url + '/api/regen_thumbs?force=0', headers=config.raragi_auth)


def delete():
    print('-------------------delete-------------------')

    # 删除qbit内种子
    i = 1
    torrents = qbt_client.torrents_info()
    length = str(len(torrents))
    for torrent in torrents:
        if torrent.category == 'autoehentai' or torrent.category == 'specialehentai' or torrent.category == 'ehentai':
            if 'fatel' in torrent.tags:
                qbt_client.torrents_delete(delete_files=True, torrent_hashes=torrent.hash)
                time.sleep(0.2)
                print('deleted_fatel_torrent:' + str(i) + '/' + length + ':' + torrent.name)
                i += 1
                continue
            if sql_manager.is_need_to_delete_torrent(torrent.hash):
                qbt_client.torrents_delete(torrent_hashes=torrent.hash)
                time.sleep(0.2)
                print('deleted_torrent:' + str(i) + '/' + length + ':' + torrent.name)
            i += 1

    # 删除aria2下载记录
    if use_aria2:
        downloads = aria2.get_downloads()
        success_downloads = [d for d in downloads if d.is_complete]
        total = len(success_downloads)
        i = 1
        for download in success_downloads:
            file_name = download.name
            match = re.search(r'^\[(\d+)]', file_name)
            if match:
                idnum = match.group(1)
                if sql_manager.is_need_to_delete_file(idnum):
                    download.remove()

                    print(f"aria2 record deleted {i}/{total}: {file_name}")
                    time.sleep(0.2)

        i += 1

    # 删除种子下载文件
    contents = os.listdir(config.torrent_download_path)
    i = 1
    length = str(len(contents))
    for item in contents:
        if sql_manager.is_need_to_delete_file(item):
            file_path = os.path.join(config.torrent_delete_path, item)
            shutil.rmtree(file_path, ignore_errors=True)
            time.sleep(0.2)
            print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1

    # 删除种子压缩文件
    contents = os.listdir(config.torrent_zip_path)
    i = 1
    length = str(len(contents))
    for item in contents:
        idnum = re.search(r'^\[(\d+)]', item)[1]
        if sql_manager.is_need_to_delete_file(idnum):
            file_path = os.path.join(config.torrent_zip_delete_path, item)
            os.remove(file_path)
            time.sleep(0.2)
            print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1

    # 删除hah下载文件
    contents = os.listdir(config.hah_download_path)
    i = 1
    length = str(len(contents))
    for item in contents:
        idnum = re.search(r'\[(\d+)]$', item)[1]
        if sql_manager.is_need_to_delete_file(idnum):
            file_path = os.path.join(config.hah_download_path, item)
            shutil.rmtree(file_path, ignore_errors=True)
            time.sleep(0.2)
            print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1

    # 删除hah压缩文件
    contents = os.listdir(config.hah_zip_path)
    i = 1
    length = str(len(contents))
    for item in contents:
        idnum = re.search(r'^\[(\d+)]', item)[1]
        if sql_manager.is_need_to_delete_file(idnum):
            file_path = os.path.join(config.hah_zip_delete_path, item)
            os.remove(file_path)
            time.sleep(0.2)
            print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1

    # 删除直接下载文件
    contents = os.listdir(config.direct_download_path)
    i = 1
    length = str(len(contents) - 1)
    for item in contents:
        if item == '[0]temp':
            continue
        idnum = re.search(r'^\[(\d+)]', item)[1]
        if sql_manager.is_need_to_delete_file(idnum):
            file_path = os.path.join(config.direct_delete_path, item)
            os.remove(file_path)
            time.sleep(0.2)
            print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1

    # 删除aria2下载文件
    contents = os.listdir(config.aria2_download_path)
    i = 1
    length = str(len(contents) - 1)
    for item in contents:
        req=re.search(r'^\[(\d+)]', item)
        if req:
            idnum = req[1]
            if sql_manager.is_need_to_delete_file(idnum):
                file_path = os.path.join(config.aria2_delete_path, item)
                os.remove(file_path)
                time.sleep(0.2)
                print('deleted ' + str(i) + '/' + length + ':' + file_path)
        i += 1


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--main", action="store_true")
    parser.add_argument("--old", action="store_true")
    parser.add_argument("--special", action="store_true")
    parser.add_argument("--noaria2", action="store_true")

    parser.add_argument("--interval", type=int, default=3600)

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

    if args.noaria2:
        use_aria2 = False
        print("no aria2")
    else:
        use_aria2 = True
        print("use aria2")
        aria2 = aria2p.API(aria2p.Client(**config.aria2_rpc))

    sql_manager = SqlManager(run_mode)

    qbt_client = qbittorrentapi.Client(**config.qbit_login)
    qbt_client.auth_log_in()

    if run_mode == "main":
        delete_log()
        complete_torrent()
        complete_hah()
        compress_torrent()
        compress_hah()
        collect_torrent()
        delete_outdate()
        handle_conflicts()
        upload_all()
        delete()
        print('done')
    else:
        while 1:
            complete_torrent()
            complete_hah()
            compress_torrent()
            compress_hah()
            collect_torrent()
            upload_all()
            delete()

            print('done')
            time.sleep(args.interval)
