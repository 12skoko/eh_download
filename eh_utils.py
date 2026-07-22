import hashlib
import os
import re
import html
import datetime
import time
import zipfile

from model import Manga, MangaInfo


def get_realname(name):
    length = len(name)
    s = 0
    m = 0
    l = 0
    r = length
    for i in range(length):
        ch = name[i]
        if (s == 0 and m == 0) and (ch != '(' and ch != '[' and ch != ' '):
            l = i
            break
        elif ch == '(':
            s += 1
        elif ch == '[':
            m += 1
        elif ch == ')':
            s -= 1
        elif ch == ']':
            m -= 1
    for i in range(length - 1, -1, -1):
        ch = name[i]
        if (s == 0 and m == 0) and (ch != ')' and ch != ']' and ch != ' '):
            r = i + 1
            break
        elif ch == ')':
            s += 1
        elif ch == ']':
            m += 1
        elif ch == '(':
            s -= 1
        elif ch == '[':
            m -= 1
    realname = name[l:r]
    return realname


def cal_rating(a, b):
    rating = (5 - int(a) // 16) * 10
    if b == '21':
        rating -= 5
    return rating


def tag_parse(tag_soup):
    if tag_soup is None:
        return ""
    tag_list = []
    for tr in tag_soup.find_all("tr", recursive=False):
        list_td = tr.find_all("td", recursive=False)
        tag_part = [div.get_text(strip=True) for div in list_td[1]]
        tag_list.append(list_td[0].text + ",".join(tag_part))
    return ",".join(tag_list)


def parse_metadata(tr_soup, mode="Extended"):
    item_metadata = Manga()
    if mode == "Extended":
        div_soup = tr_soup.find("td", class_="gl2e").find("div")
        metadata_soup = div_soup.find("div", class_="gl3e")
        title_tag_soup = metadata_soup.find_next_sibling("a")

        link = title_tag_soup["href"]
        manga_id = re.search(r"\.org/g/(\d+/\w+)/?", link)[1]

        name_soup = title_tag_soup.find("div", class_="glink")
        name = name_soup.text
        realname = get_realname(name)

        tag = tag_parse(name_soup.find_next_sibling("div").find("table"))

        list_metadata_div_soup = metadata_soup.find_all("div", recursive=False)

        category = list_metadata_div_soup[0].text
        postedtime = list_metadata_div_soup[1].text
        postedtimestamp = int(datetime.datetime.strptime(postedtime, "%Y-%m-%d %H:%M").timestamp())

        rating_style = list_metadata_div_soup[2]["style"]
        rating_args = re.match(r"background-position:-?(\d+)px -(\d+)px;opacity:", rating_style)

        rating = cal_rating(rating_args[1], rating_args[2])

        uploader = list_metadata_div_soup[3].text
        pages = int(list_metadata_div_soup[4].text.split(' ')[0])

        torrent_soup = list_metadata_div_soup[5].find("a")
        if torrent_soup is not None:
            torrentlink = torrent_soup["href"]
        else:
            torrentlink = ""

        item_metadata.manga_id = manga_id
        item_metadata.name = name
        item_metadata.link = link
        item_metadata.realname = realname
        item_metadata.category = category
        item_metadata.postedtime = postedtime
        item_metadata.postedtimestamp = postedtimestamp
        item_metadata.rating = rating
        item_metadata.uploader = uploader
        item_metadata.pages = pages
        item_metadata.torrentlink = torrentlink
        item_metadata.tag = tag
        item_metadata.fetchtime = datetime.datetime.now()

        return item_metadata


def parse_info(soup, tag_trans):
    name = soup.find("h1", id="gj").text.replace('"', '""')
    name = html.unescape(name).replace('"', '""')
    romaname = soup.find("h1", id="gn").text.replace('"', '""')
    realname = get_realname(name)
    if name == '':
        name = romaname
        romaname = ''
    category = soup.find("div", id="gdc").text
    uploader = soup.find("div", id="gdn").text
    bs_td = soup.find_all("td", class_="gdt2")
    postedtime = bs_td[0].text
    parent = bs_td[1].text
    language = bs_td[3].text.replace("\xa0", "")
    estimatedsize = bs_td[4].text
    pages = int(bs_td[5].text.replace(" pages", ""))
    favorited = int(bs_td[6].text.replace(" times", "").replace("Once", "1").replace("Never", "0"))
    rating_count = int(soup.find("span", id="rating_count").text)
    rating = int(float(soup.find("td", id="rating_label").text.replace("Average: ", "")) * 100)
    tagstrings = soup.find("div", id="taglist")
    taglist = []
    row = ''
    for text in tagstrings.strings:
        if ':' in text:
            row = text
        else:
            taglist.append(row + text)
    tag = ','.join(taglist)
    tag_tran = tag_trans.get_trans(tag).replace('"', '""')
    onclick_value = soup.find('a', href="#", string='Archive Download').get('onclick')
    downloadlink = re.search(r"return popUp\('(https://exhentai\.org/archiver\.php.*?)',480,320\)", onclick_value)[1]

    item_info = MangaInfo()

    item_info.name = name
    item_info.romaname = romaname
    item_info.realname = realname
    item_info.category = category
    item_info.uploader = uploader
    item_info.postedtime = postedtime
    item_info.language = language
    item_info.estimatedsize = estimatedsize
    item_info.pages = pages
    item_info.favorited = favorited
    item_info.ratingcount = rating_count
    item_info.rating = rating
    item_info.tag = tag
    item_info.tagtran = tag_tran

    item_info.fetchtime = int(time.time())

    return item_info, downloadlink, parent


def is_filename_too_long(filename, max_bytes=255, encoding="utf-8"):
    try:
        encoded_length = len(filename.encode(encoding))
        return encoded_length > max_bytes
    except UnicodeEncodeError:
        return True


def parse_file_size(size_str):
    units = {"B": 1, "KiB": 1024, "MiB": 1048576, "GiB": 1073741824, "TiB": 1099511627776}
    size, unit = float(size_str[:-4]), size_str[-3:]
    return size * units[unit]


def check_complete(base_directory, partial_directory_name, file_name="galleryinfo.txt"):
    for item in os.listdir(base_directory):
        item_path = os.path.join(base_directory, item)
        if os.path.isdir(item_path) and partial_directory_name in item:
            for file_item in os.listdir(item_path):
                if os.path.isfile(os.path.join(item_path, file_item)) and file_item == file_name:
                    return True, item
    return False, ""


def calculate_sha1(file_path):
    sha1 = hashlib.sha1()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            sha1.update(chunk)
    return sha1.hexdigest()


def is_folder_all_files(folder_path):
    for item in os.listdir(folder_path):
        item_path = os.path.join(folder_path, item)
        if os.path.isdir(item_path):
            return False
    return True


def create_zip_file(folder_path, zip_file_name):
    with zipfile.ZipFile(zip_file_name, 'w', zipfile.ZIP_STORED) as zip_file:
        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                file_path = str(file_path)
                zip_file.write(file_path, os.path.relpath(file_path, folder_path))


def get_folder_name(base_dir, partial_name):
    for dirpath, dirnames, filenames in os.walk(base_dir):
        for dirname in dirnames:
            if partial_name in dirname:
                return os.path.join(dirpath, dirname)
    return None


def select(lists):
    sorted_list = sorted(lists, key=lambda x: x[0], reverse=True)
    return sorted_list[0][1]


def screen(similarFlagList):
    res = [0] * len(similarFlagList)
    filterDict1 = {1: [], 2: [], 3: []}
    for i in range(len(similarFlagList)):
        similarFlag = similarFlagList[i]
        filterDict1[similarFlag // 10].append((round(similarFlag - (similarFlag // 10) * 10, 12), i))
    if filterDict1[3] != []:
        filterDict2 = filterDict1[3]
    elif filterDict1[2] != []:
        filterDict2 = filterDict1[2]
    else:
        filterDict2 = filterDict1[1]
    filterDict3 = {}
    for i in filterDict2:
        if int(i[0]) in filterDict3:
            filterDict3[int(i[0])].append((round(i[0] - int(i[0]), 12), i[1]))
        else:
            filterDict3[int(i[0])] = [(round(i[0] - int(i[0]), 12), i[1])]
    for i in filterDict3:
        res[select(filterDict3[i])] = 1
    return res


def contains_key(text, keywords):
    pattern = r'\b' + re.escape(keywords) + r'\b'
    return bool(re.search(pattern, text))


def judge_screen_flag(manga_metadata: Manga, name_keywords, tag_keywords):
    languages = ['english', 'korean', 'russian', 'french', 'dutch', 'hungarian', 'italian', 'polish', 'portuguese', 'spanish', 'thai', 'vietnamese', 'ukrainian']
    if 'translated' in manga_metadata.tag and 'chinese' not in manga_metadata.tag:
        if any(lang in manga_metadata.tag for lang in languages):
            screen_flag = 0
            return screen_flag

    if any(contains_key(manga_metadata.name.lower(), name_keyword) for name_keyword in name_keywords):
        screen_flag = 2
        return screen_flag
    if any(contains_key(manga_metadata.tag, tag_keyword) for tag_keyword in tag_keywords):
        screen_flag = 2
        return screen_flag

    if (manga_metadata.category == "Manga" or manga_metadata.category == "Doujinshi") and ("chinese" in manga_metadata.tag or manga_metadata.rating >= 30):

        nowtimestamp = int(time.time())
        manga_timestamp = int(datetime.datetime.strptime(manga_metadata.postedtime, "%Y-%m-%d %H:%M").timestamp())
        if nowtimestamp - manga_timestamp > 86400:
            screen_flag = 1
            return screen_flag
        else:
            screen_flag = -1
            return screen_flag
    else:
        screen_flag = 0
        return screen_flag


def updateTagTranslation():
    import requests
    import config
    url = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json"
    target_file = "./db.text.json"
    temp_file = target_file + ".tmp"

    try:
        response = requests.get(url, stream=True, proxies=config.proxies1)
        response.raise_for_status()

        with open(temp_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if os.path.exists(temp_file):
            if os.path.exists(target_file):
                os.replace(temp_file, target_file)
            else:
                os.rename(temp_file, target_file)
        print(f"文件已更新：{target_file}")

    except Exception as e:
        if os.path.exists(temp_file):
            os.remove(temp_file)
        print(f"下载失败: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="工具函数集")
    parser.add_argument("function", choices=["updateTagTranslation"], help="要运行的函数名")

    args = parser.parse_args()

    if args.function == "updateTagTranslation":
        updateTagTranslation()
