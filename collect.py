import argparse
from sqlalchemy import create_engine, select, update, insert, MetaData, Table, desc, and_, or_
from sqlalchemy.orm import sessionmaker
from model import Manga
import config
import eh_utils
import requests
from bs4 import BeautifulSoup
import datetime
import time
import re
import random
import os


def getRandom():
    metadata = MetaData()
    random_table = Table('random', metadata, autoload_with=engine)
    with engine.begin() as conn:
        select_stmt = (
            select(random_table.c.id)
            .where(random_table.c.is_used == 0)  # type: ignore
            .limit(1)
        )
        result = conn.execute(select_stmt)
        num = result.scalar()
        if num is None:
            raise ValueError("No available unused random IDs")
        update_stmt = (
            update(random_table)
            .where(random_table.c.id == num)  # type: ignore
            .values(is_used=1)
        )
        conn.execute(update_stmt)
        return num


def screenall():
    with SqlSession() as sql_session:
        undetermined_all_book = sql_session.query(Manga).filter(Manga.autostate == 1).all()  # type: ignore

        co = 0
        length = str(len(undetermined_all_book))

        for manga in undetermined_all_book:
            co += 1
            print(str(co) + '/' + length)
            similarList = sql_session.query(Manga).filter(and_(Manga.realname == manga.realname, or_(Manga.category == "Manga", Manga.category == "Doujinshi"))).all()  # type: ignore
            if len(similarList) == 1:
                manga.autostate = 2
                sql_session.commit()
            else:
                similarFlagList = []
                for similar in similarList:
                    score = similar.rating * 0.01 + similar.postedtimestamp * 0.000000000001
                    if "無修正" in similar.name or "无修正" in similar.name:
                        if 'chinese' in similar.tag:
                            if similar.rating > 30:
                                similarFlagList.append(31 + score)
                            else:
                                similarFlagList.append(22 + score)
                        else:
                            similarFlagList.append(21 + score)
                    else:
                        if 'chinese' in similar.tag:
                            if similar.rating > 30:
                                similarFlagList.append(23 + score)
                            else:
                                similarFlagList.append(12 + score)
                        else:
                            similarFlagList.append(11 + score)

                res = eh_utils.screen(similarFlagList)
                random_num = getRandom()
                for i in range(len(res)):
                    if not (similarList[i].autostate != 1 or similarList[i].state is not None):
                        if res[i] == 1:
                            similarList[i].autostate = 2
                        else:
                            similarList[i].autostate = 3
                    similarList[i].relatetation = str(random_num)
                    sql_session.commit()


def collect(base_url, start, end, mark):
    se = requests.session()
    if start != 0:
        url = base_url + "&next=" + str(start)
    else:
        url = base_url
    dev = 0
    error_flag = 0
    now_page = 0
    next_num = 9999999999
    while next_num > end:
        proxy = config.proxy_pool[(datetime.datetime.now().hour + dev) % len(config.proxy_pool)]
        print(mark + ':' + str(now_page))
        print(url)
        old_url = url
        try:
            response = se.get(url, headers=config.header, cookies=config.cookies_non_donation, proxies=proxy)
            data_soup = BeautifulSoup(response.text, 'lxml')

            unext_a_soup = data_soup.find("a", id="unext")
            if unext_a_soup is None:
                unext_span_soup = data_soup.find("span", id="unext")
                if unext_span_soup is None:
                    print(response.text)
                    raise "request error"
                else:
                    next_num = 0
            else:
                url = unext_a_soup.get("href")
                next_num = int(url.split("next=")[1].split("&")[0])
        except Exception as e:
            print('request error')
            print(e)
            error_flag += 1
            url = old_url
            dev += 1
            if error_flag >= len(config.proxy_pool):
                raise 'request error'
            else:
                time.sleep(2)
                continue

        error_flag = 0

        total_info_soup = data_soup.find("table", class_="itg glte")
        list_tr_soup = total_info_soup.find_all("tr", recursive=False)
        print('find ', len(list_tr_soup))
        if len(list_tr_soup) != 25 and next_num != 0:
            raise 'find metadata error'

        for tr_soup in list_tr_soup:
            manga_metadata = eh_utils.parse_metadata(tr_soup)
            screen_flag = eh_utils.judge_screen_flag(manga_metadata, config.name_keywords, config.tag_keywords)

            with SqlSession() as sql_session:
                existing_record = sql_session.get(Manga, manga_metadata.manga_id)
                if existing_record and existing_record.autostate != -1:
                    manga_metadata.autostate = existing_record.autostate
                    manga_metadata.state = existing_record.state
                else:
                    if screen_flag == -1:
                        manga_metadata.autostate = -1
                    elif screen_flag == 1:
                        manga_metadata.autostate = 1
                    elif screen_flag == 2:
                        manga_metadata.autostate = 2
                    elif screen_flag == 0:
                        manga_metadata.state = 1
                        manga_metadata.autostate = None

                sql_session.merge(manga_metadata)
                sql_session.commit()

        print("Insert success")

        now_page += 1
        time.sleep(5 + random.randint(0, 10))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=int, help="手动指定 end 数值")
    args = parser.parse_args()

    engine = create_engine(config.sql_engine)
    SqlSession = sessionmaker(bind=engine)

    if args.end is not None:
        end = int(args.end)
        print(f"使用命令行指定的 end 值: {end}")
    else:
        with SqlSession() as sql_session:
            now = datetime.datetime.now()

            results = sql_session.query(Manga).filter(
                Manga.autostate == -1  # type: ignore
            ).order_by(Manga.postedtimestamp.asc()).all()

            few_days_ago = datetime.datetime.now() - datetime.timedelta(days=config.few_days_ago)
            few_days_ago_timestamp = int(few_days_ago.timestamp())

            selected_result = None
            for result in results:
                if result.postedtimestamp <= few_days_ago_timestamp:
                    continue
                else:
                    selected_result = result
                    break

            if selected_result:
                latest_id_str = selected_result.manga_id
                init_end = int(latest_id_str.split('/')[0]) - 3000
                end = init_end
                print(f"使用自动获取的 end 值: {end}")
            else:
                print("未找到end")
                end = 0

    for collect_url in config.collect_url_list:
        collect(collect_url, 0, end, config.collect_url_list[collect_url])

    screenall()

    try:
        eh_utils.updateTagTranslation()
    except Exception as e:
        print(e)
        print('更新标签数据库失败')
    print('done')
