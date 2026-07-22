import argparse
import time
import requests
import re
import random
import config
import datetime
from bs4 import BeautifulSoup
import html
import os
from tqdm import tqdm
import shutil
from sqlalchemy import create_engine, select, update, desc, Nullable, MetaData, Table, insert
from sqlalchemy.orm import sessionmaker
from model import Manga, MangaInfo, EhTagTranslation
import eh_utils
import aria2p


class SqlManager():
    def __init__(self, run_mode):
        self.engine = create_engine(config.sql_engine)
        self.SqlSession = sessionmaker(bind=self.engine)
        self.run_mode = run_mode

    def select_download_hah(self):
        with self.SqlSession() as sql_session:
            if self.run_mode == "main":
                query = (sql_session.query(Manga)
                         .filter(Manga.autostate == 6)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "old":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 6)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            elif self.run_mode == "special":
                query = (sql_session.query(Manga)
                         .filter(Manga.state == 15)  # type: ignore
                         .order_by(desc(Manga.postedtimestamp)))
            else:
                raise ValueError(f"Unknown run_mode: {run_mode}")
            return query.all()

    def post_hah_download_success(self, remark, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 7
            elif self.run_mode == "old":
                manga.state = 9
            elif self.run_mode == "special":
                manga.state = 9
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.remark = remark

            sql_session.commit()

    def direct_download_success(self, filename, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 11
            elif self.run_mode == "old":
                manga.state = 11
            elif self.run_mode == "special":
                manga.state = 11
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.filename = filename

            sql_session.commit()

    def direct_download_failed(self, manga_id, massage):
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
            manga.remark = massage

            sql_session.commit()

    def filename_too_long(self, filename, manga_id):
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
            manga.remark = f"filename too long: {filename}"

            sql_session.commit()

    def complete_hah_download(self, alias, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            if self.run_mode == "main":
                manga.autostate = 9
            elif self.run_mode == "old":
                manga.state = 10
            elif self.run_mode == "special":
                manga.state = 10
            else:
                raise ValueError(f"Unknown run_mode: {self.run_mode}")
            manga.alias = alias

            sql_session.commit()

    def download_failed_due_to_copyright(self, manga_id):
        with self.SqlSession() as sql_session:
            manga = sql_session.get(Manga, manga_id)

            manga.state = 4
            manga.autostate = None
            manga.remark = "This gallery is unavailable due to a copyright claim"

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

    def update_gp(self, size, gp):
        gp_table = Table('GP', MetaData(), autoload_with=self.engine)
        with self.SqlSession() as sql_session:
            today = datetime.date.today()
            year, week, _ = today.isocalendar()
            weeknum = year * 100 + week

            select_stmt = select(gp_table).where(gp_table.c.week == weeknum)  # type: ignore
            req = sql_session.execute(select_stmt).first()

            if not req:
                update_stmt = (update(gp_table)
                               .where(gp_table.c.week == weeknum - 1)  # type: ignore
                               .values(nowgp=gp)
                               )
                sql_session.execute(update_stmt)

                insert_stmt = (
                    insert(gp_table)
                    .values(week=weeknum, quota=size)
                )
                sql_session.execute(insert_stmt)

            else:
                file_sizes = [req.quota, size]
                total_size = sum(eh_utils.parse_file_size(size) for size in file_sizes)
                total_size_mb = str(int(total_size / (1024 ** 2))) + ' MiB'

                update_stmt = (update(gp_table)
                               .where(gp_table.c.week == weeknum)  # type: ignore
                               .values(quota=total_size_mb)
                               )
                sql_session.execute(update_stmt)

            sql_session.commit()


def download_file(url, filename, download_path, retries=3, min_speed=config.direct_download_min_speed, check_interval=5):
    total, used, free = shutil.disk_usage(download_path)
    if free < 3221225472:
        print('空间不足3GB，中断下载')
        raise 'not enough space'
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, headers=config.header, stream=True, proxies=config.proxies1)
            if response.status_code == 410:
                print('410 error')
                return -1
            response.raise_for_status()
            total_size = int(response.headers.get('content-length', 0))
            block_size = 1024

            progress_bar = tqdm(total=total_size, unit='iB', unit_scale=True, position=0, ncols=60)
            start_time = time.time()
            downloaded_size = 0

            filepath = os.path.join(download_path, filename)
            temp_filepath = os.path.join(download_path, '[0]temp/', filename)

            with open(temp_filepath, 'wb') as file:
                for data in response.iter_content(block_size):
                    file.write(data)
                    progress_bar.update(len(data))
                    downloaded_size += len(data)

                    # 检查下载速度
                    elapsed_time = time.time() - start_time
                    if elapsed_time >= check_interval:
                        speed = (downloaded_size / 1024) / elapsed_time  # kB/s
                        if speed < min_speed:
                            raise Exception(f"Download speed too low: {speed:.2f} kB/s")
                        start_time = time.time()  # 重置计时
                        downloaded_size = 0

                progress_bar.close()

            if total_size != 0 and progress_bar.n != total_size:
                raise Exception("ERROR: download error - file size mismatch")

            shutil.move(temp_filepath, filepath)  # type: ignore
            print("Download completed successfully.")
            return 1

        except (ConnectionError, Exception) as e:
            attempt += 1
            try:
                progress_bar.close()
            except:
                pass
            print(f"Download attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                print("Retrying...")
                time.sleep(200)
            else:
                raise "Max retries reached. Raising error to terminate program."


def download_aria2(url, filename, download_path):
    download = aria2.add_uris([url], options={"dir": download_path, "out": filename})

    pbar = tqdm(total=0, unit='iB', unit_scale=True, unit_divisor=1024, position=0, ncols=60)

    last_completed = 0

    while not download.is_complete:
        download.update()

        if download.has_failed:
            pbar.close()
            print(f"\n下载失败: {download.error_message}")
            return download.error_message

        if pbar.total == 0 and download.total_length > 0:
            pbar.total = download.total_length

        current_completed = download.completed_length
        pbar.update(current_completed - last_completed)
        last_completed = current_completed

        time.sleep(0.5)

    pbar.close()
    print("文件下载成功")
    return 1


def determine_download_method(soup):
    direct_cost = soup.find("div", style="width:180px; float:left").find("div", style="text-align:center; margin-top:4px").find("strong").text
    if direct_cost == 'Free!':
        direct_cost = 0
    else:
        direct_cost = int(direct_cost[:-3].replace(',', ''))
    target_td = ''
    for td in soup.find_all('td'):
        p_tags = td.find_all('p')
        if p_tags and p_tags[0].text.strip() == 'Original':
            target_td = td
            break
    hah_cost = target_td.find_all('p')[2].text
    if hah_cost == 'Free':
        hah_cost = 0
    else:
        hah_cost = int(hah_cost[:-3].replace(',', ''))

    downflag = 1
    if direct_cost == 0 or direct_cost < hah_cost or hah_cost > 8000 or hah_cost < 400:
        downflag = 0

    return downflag


def download_hah(run_mode, download_mode):
    se = requests.session()

    tagTrans = EhTagTranslation()

    mangaList = sql_manager.select_download_hah()
    lense = len(mangaList)
    dev = 0
    i = 0
    errorflag = 0
    proxy = config.proxies1
    while i < lense:
        # proxy = config.proxyPool[(datetime.datetime.now().hour + dev) % len(config.proxyPool)]
        manga = mangaList[i]
        print(str(i + 1) + '/' + str(lense))
        url = manga.link
        try:
            data = se.get(url, headers=config.header, cookies=config.cookies_with_donation, proxies=proxy).text
            if 'This gallery is unavailable due to a copyright claim' in data:
                print('This gallery is unavailable due to a copyright claim')
                sql_manager.download_failed_due_to_copyright(manga.manga_id)
                i += 1
                time.sleep(10)
                continue

            soup = BeautifulSoup(data, 'lxml')
            manga_info, downloadlink, parent = eh_utils.parse_info(soup, tagTrans)
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

        downpage = se.get(downloadlink, headers=config.header, cookies=config.cookies_with_donation, proxies=proxy).text
        soup2 = BeautifulSoup(downpage, 'lxml')

        downflag = determine_download_method(soup2)
        if download_mode == 'hah':
            downflag = 0
        elif download_mode == 'direct':
            downflag = 1

        postlink = downloadlink.replace('--', '-')
        if downflag == 0:
            print('hah download')
            req2 = se.post(postlink, headers=config.header, cookies=config.cookies_with_donation, data={'hathdl_xres': 'org'}, proxies=proxy).text
            if 'An original resolution download has been queued for client' not in req2:
                print(postlink)
                print(req2)
                raise 'hah post error'

            sql_manager.post_hah_download_success("Post hah download: " + str(time.time()), manga.manga_id)

        else:
            print('direct download')
            print(manga.manga_id)
            data = se.post(postlink, headers=config.header, cookies=config.cookies_with_donation,
                           data={'dltype': 'org', 'dlcheck': 'Download Original Archive'}, proxies=proxy).text
            # print(data)
            soup3 = BeautifulSoup(data, 'lxml')
            templink = soup3.find("p", id="continue").find('a')['href']
            downlink = templink + '?start=1'
            idname = '[' + manga.manga_id.split('/')[0] + ']'
            if idname in config.too_long_name_list:
                zipname = config.too_long_name_list[idname]
            else:
                zipname = '[' + manga.manga_id.split('/')[0] + ']' + re.sub(r'[\\/*?:"<>|]', '_', manga.name) + '.zip'
            if eh_utils.is_filename_too_long(zipname):
                print('File name too long: ' + zipname)
                sql_manager.filename_too_long(zipname, manga.manga_id)
            else:
                print(downlink)
                print(zipname)
                if use_aria2:
                    flag = download_aria2(downlink, zipname, config.aria2_rpc_download_path)
                else:
                    flag = download_file(downlink, zipname, config.direct_download_path)

                if flag == 1:
                    sql_manager.direct_download_success(zipname, manga.manga_id)
                else:
                    sql_manager.direct_download_failed(manga.manga_id, flag)
                    raise 'direct download error'

        if run_mode == "main":
            sql_manager.parent_outdate(parent)

        gpreq = se.get('https://e-hentai.org/exchange.php?t=gp',
                       headers=config.header,
                       cookies=config.cookies_with_donation,
                       proxies=proxy).text
        gpstr = re.search('Available: (.*?) kGP', gpreq)[1]
        gp = int(gpstr.replace(',', '')) * 1000
        sql_manager.update_gp(manga_info.estimatedsize, gp)

        print(manga.manga_id)
        i += 1

        if i < lense:
            time.sleep(20 + random.randint(0, 40))
            if downflag == 0:
                i_time = 0
                partial_name = '[' + manga.manga_id.split('/')[0] + ']'
                while i_time < 80:
                    time.sleep(20)
                    flag = eh_utils.check_complete(config.hah_download_path, partial_name)
                    if flag[0] == True:
                        sql_manager.complete_hah_download(flag[1], manga.manga_id)
                        print('completeHah', manga.manga_id)
                        break
                    i_time += 1

    print('done')


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--main", action="store_true")
    parser.add_argument("--old", action="store_true")
    parser.add_argument("--special", action="store_true")
    parser.add_argument("--hah", action="store_true")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--noaria2", action="store_true")

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

    if args.hah == True and args.direct == True:
        raise 'wrong args'
    if args.hah:
        download_mode = 'hah'
    elif args.direct:
        download_mode = 'direct'
    else:
        download_mode = 'direct'

    if args.noaria2:
        use_aria2 = False
        print("no aria2")
    else:
        use_aria2 = True
        print("use aria2")
        aria2 = aria2p.API(aria2p.Client(**config.aria2_rpc))

    sql_manager = SqlManager(run_mode)

    download_hah(run_mode, download_mode)
