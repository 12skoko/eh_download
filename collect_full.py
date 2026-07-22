from sqlalchemy import create_engine, select, update
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


def collect(base_url, start, end, mark):
    se = requests.session()
    if start != '':
        url = base_url + "&next=" + start
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
            response = se.get(url, headers=config.header, cookies=config.cookies_with_donation, proxies=proxy)
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
                with open('full_collect_log.txt', 'a', encoding='utf-8') as file:
                    file.write(str(next_num) + '\n')
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
        if len(list_tr_soup) != 100 and next_num != 0:
            raise 'find metadata error'

        for tr_soup in list_tr_soup:
            manga_metadata = eh_utils.parse_metadata(tr_soup)

            with SqlSession() as sql_session:
                existing_record = sql_session.get(Manga, manga_metadata.manga_id)
                if existing_record and existing_record.autostate != -1:
                    manga_metadata.autostate = existing_record.autostate
                    manga_metadata.state = existing_record.state
                else:
                    manga_metadata.state = 13
                sql_session.merge(manga_metadata)
                sql_session.commit()

        print("Insert success")

        now_page += 1
        time.sleep(10 + random.randint(0, 20))


if __name__ == "__main__":
    engine = create_engine(config.sql_engine)
    SqlSession = sessionmaker(bind=engine)

    start = input("start: ")

    collect(config.collect_full_url, start, 0, "full")

    print('done')
