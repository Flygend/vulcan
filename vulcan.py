#!/usr/bin/env python
# coding:utf-8
# code by pnig0s@20140131

'''
Vulcan Spider @ 2014

基于gevent和多线程模型，支持WebKit引擎的DOM解析动态爬虫框架。
框架由两部分组成：
fetcher:下载器，负责获取HTML，送入crawler。
crawler:爬取器，负责解析并爬取HTML中的URL，送入fetcher。
fetcher和crawler两部分独立工作，互不干扰，通过queue进行链接
fetcher需要发送HTTP请求，涉及到阻塞操作，使用gevent池控制
crawler没有涉及阻塞操作，但为了扩展可以自选gevent池和多线程池两种模型控制

'''

import gevent
from gevent import monkey
from gevent import Greenlet
from gevent import pool
from gevent import queue
from gevent import event
from gevent import Timeout
from gevent import threadpool

from exceptions import *
from plugin import *
from contextlib import closing

monkey.patch_all()
import re
import os
import sys
import time
import uuid
import string
import urlparse
import logging

import requests
import chardet

from Data import UrlCache, UrlData
from utils import HtmlAnalyzer

try:
    from utils import WebKit
except Exception, e:
    pass


def monkey_patch():
    '''
    requests库中文乱码补丁
    '''
    prop = requests.models.Response.content

    def content(self):
        _content = prop.fget(self)
        if self.encoding == 'ISO-8859-1':
            encodings = requests.utils.get_encodings_from_content(_content)
            if encodings:
                self.encoding = encodings[0]
            else:
                self.encoding = self.apparent_encoding
            _content = _content.decode(self.encoding, 'replace').encode('utf8', 'replace')
            self._content = _content
        return _content

    requests.models.Response.content = property(content)


monkey_patch()


def to_unicode(data, charset=None):
    '''
    将输入的字符串转化为unicode对象
    '''
    unicode_data = ''
    if isinstance(data, str):
        if not charset:
            try:
                charset = chardet.detect(data).get('encoding')
            except Exception, e:
                pass
        if charset:
            unicode_data = data.decode(charset, 'ignore')
        else:
            unicode_data = data
    else:
        unicode_data = data
    return unicode_data


#   Greenlet的子类
class Fetcher(Greenlet):
    """抓取器(下载器)类"""

    #   重写__init__函数
    def __init__(self, spider):
        Greenlet.__init__(self)
        self.fetcher_id = str(uuid.uuid1())[:8]
        self.TOO_LONG = 1048576  # 1M
        self.spider = spider
        self.fetcher_cache = self.spider.fetcher_cache
        self.crawler_cache = self.spider.crawler_cache
        self.fetcher_queue = self.spider.fetcher_queue
        self.crawler_queue = self.spider.crawler_queue
        self.logger = self.spider.logger

    def _fetcher(self): #   私有方法
        '''
        抓取器主函数
        '''
        self.logger.info("fetcher %s starting...." % (self.fetcher_id,))

        #   如果 stopped 事件没有被置位
        while not self.spider.stopped.isSet():
            try:
                url_data = self.fetcher_queue.get(block=False)  #   从下载队列取出一个URL
                #    self.logger.info("url_data %s" % url_data)
                #    (block is false), return an item if one is immediately available
                #    if block is true and timeout is None (the default), block if necessary until an item is available
                #    If timeout is a positive number, it blocks at most timeout
            except queue.Empty, e:
                #   如果停止爬取并且全部下载完毕
                if self.spider.crawler_stopped.isSet() and self.fetcher_queue.unfinished_tasks == 0:
                    self.spider.stop()
                #   如果全部处理完毕
                elif self.crawler_queue.unfinished_tasks == 0 and self.fetcher_queue.unfinished_tasks == 0:
                    self.spider.stop()
                else:
                    gevent.sleep()
            else:
                #   如果html数据为空
                if not url_data.html:
                    try:
                        #   避免重复爬取
                        if url_data not in set(self.crawler_cache):
                            html = ''
                            with gevent.Timeout(self.spider.internal_timeout, False) as timeout:
                                html = self._open(url_data) #   获取HTML内容
                            if not html.strip():    #   如果返回数据为空
                                self.spider.fetcher_queue.task_done()
                                continue
                            self.logger.info("fetcher %s accept %s" % (self.fetcher_id, url_data))

                            #   将HTML加入解析爬取队列
                            url_data.html = html
                            if not self.spider.crawler_stopped.isSet():
                                self.crawler_queue.put(url_data, block=True)
                            self.crawler_cache.insert(url_data) #   加入到处理完毕队列
                    except Exception, e:
                        import traceback
                        traceback.print_exc()
                self.spider.fetcher_queue.task_done()   #   下载队列取出的URL处理完毕

    def _open(self, url_data):
        '''
        获取HTML内容
        '''
        human_headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/32.0.1700.76 Safari/537.36',
            'Accept-Encoding': 'gzip,deflate,sdch'
        }
        if self.spider.custom_headers:
            human_headers.update(self.spider.custom_headers)
        try:
            r = requests.get(url_data.url, headers=human_headers, stream=True)
        except Exception, e:
            self.logger.warn("%s %s" % (url_data.url, str(e)))
            return u''
        else:
            if r.headers.get('content-type', '').find('text/html') < 0:
                r.close()
                return u''
            if int(r.headers.get('content-length', self.TOO_LONG)) > self.TOO_LONG:
                r.close()
                return u''
            try:
                html = r.content
                html = html.decode('utf-8', 'ignore')
            except Exception, e:
                self.logger.warn("%s %s" % (url_data.url, str(e)))
            finally:
                r.close()
                if vars().get('html'):
                    return html
                else:
                    return u''

    #   重写_run函数，也是执行入口
    def _run(self):
        self._fetcher()


class Spider(object):
    """爬虫主类"""
    logger = logging.getLogger("spider")

    #   相当于构造函数
    def __init__(self, concurrent_num=20, crawl_tags=[], custom_headers={}, plugin=[], depth=3,
                 max_url_num=300, internal_timeout=60, spider_timeout=6 * 3600,
                 crawler_mode=0, same_origin=True, dynamic_parse=False):
        """
        concurrent_num    : 并行crawler和fetcher数量
        crawl_tags        : 爬行时收集URL所属标签列表
        custom_headers    : 自定义HTTP请求头
        plugin            : 自定义插件列表
        depth             : 爬行深度限制
        max_url_num       : 最大收集URL数量
        internal_timeout  : 内部调用超时时间
        spider_timeout    : 爬虫超时时间
        crawler_mode      : 爬取器模型(0:多线程模型,1:gevent模型)
        same_origin       : 是否限制相同域下
        dynamic_parse     : 是否使用WebKit动态解析
        """

        #   日志模块
        self.logger.setLevel(logging.DEBUG) #   日志级别
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")   #   日志格式
        hd = logging.StreamHandler()
        hd.setFormatter(formatter)
        self.logger.addHandler(hd)

        self.stopped = event.Event()
        self.internal_timeout = internal_timeout    #   内部调用超时时间
        self.internal_timer = Timeout(internal_timeout)

        self.crawler_mode = crawler_mode        #   爬取器模型
        self.concurrent_num = concurrent_num    #   并行crawler与fetcher数量

        #   fetcher使用gevent模型
        self.fetcher_pool = pool.Pool(self.concurrent_num)

        #   crawler模型设置
        #   crawler负责解析并爬取HTML中的URL，送入fetcher，fetcher负责获取HTML，送入crawler
        if self.crawler_mode == 0:
            #   线程池模型
            self.crawler_pool = threadpool.ThreadPool(min(50, self.concurrent_num))
        else:
            #   gevent模型
            self.crawler_pool = pool.Pool(self.concurrent_num)

        #   fetcher和crawler两部分独立工作，互不干扰，通过queue进行链接
        # self.fetcher_queue = queue.JoinableQueue(maxsize=self.concurrent_num*100)
        self.fetcher_queue = threadpool.Queue(maxsize=self.concurrent_num * 10000)
        self.crawler_queue = threadpool.Queue(maxsize=self.concurrent_num * 10000)

        self.fetcher_cache = UrlCache()
        self.crawler_cache = UrlCache()

        self.default_crawl_tags = ['a', 'base', 'iframe', 'frame', 'object']    #   默认的爬行时收集URL所属标签列表
        self.ignore_ext = ['js', 'css', 'png', 'jpg', 'gif', 'bmp', 'svg', 'exif', 'jpeg', 'exe', 'rar', 'zip']     #   爬行时忽略的URL种类
        self.crawl_tags = list(set(self.default_crawl_tags) | set(crawl_tags))  #   爬行时收集URL所属标签列表
        self.same_origin = same_origin      #   是否同源
        self.depth = depth                  #   爬行深度限制
        self.max_url_num = max_url_num      #   最大收集URL数量
        self.dynamic_parse = dynamic_parse  #   是否使用WebKit动态解析

        #   如果开启动态解析
        if self.dynamic_parse:
            self.webkit = WebKit()
        self.crawler_stopped = event.Event()

        self.plugin_handler = plugin  # 注册Crawler中使用的插件
        #   自定义HTTP头
        self.custom_headers = custom_headers


    def _start_fetcher(self):
        '''
        启动下载器
        '''
        for i in xrange(self.concurrent_num):   #   concurrent_num:并行数量
            fetcher = Fetcher(self)             #   实例化一个 fetcher
            self.fetcher_pool.start(fetcher)    #   调用start()开始执行_run()


    def _start_crawler(self):
        '''
        启动爬取器
        '''
        for _ in xrange(self.concurrent_num):
            self.crawler_pool.spawn(self.crawler)   #   启动self.crawler()函数


    def start(self):
        '''
        主启动函数
        '''
        self.logger.info("spider starting...")

        if self.crawler_mode == 0:
            self.logger.info("crawler run in multi-thread mode.")
        elif self.crawler_mode == 1:
            self.logger.info("crawler run in gevent mode.")

        #   开启fetcher与crawler
        self._start_fetcher()   #   初始爬取URL已在main函数中spider.feed_url(url)中设置
        self._start_crawler()

        self.stopped.wait()  # 等待停止事件置位

        #   等待fetcher与crawler执行结束
        try:
            self.internal_timer.start()
            self.fetcher_pool.join(timeout=self.internal_timer)
            if self.crawler_mode == 1:
                self.crawler_pool.join(timeout=self.internal_timer)
            else:
                self.crawler_pool.join()
        except Timeout:
            self.logger.error("internal timeout triggered")
        finally:
            self.internal_timer.cancel()

        self.stopped.clear()
        if self.dynamic_parse:
            self.webkit.close()
        self.logger.info("crawler_cache:%s fetcher_cache:%s" % (len(self.crawler_cache), len(self.fetcher_cache)))
        self.logger.info("spider process quit.")

    def crawler(self, _dep=None):
        '''
        爬行器主函数
        '''
        while not self.stopped.isSet() and not self.crawler_stopped.isSet():
            try:
                self._maintain_spider()  # 维护爬虫池
                url_data = self.crawler_queue.get(block=False)  #   从爬取队列取出一个URL
            except queue.Empty, e:  #   队列为空
                if self.crawler_queue.unfinished_tasks == 0 and self.fetcher_queue.unfinished_tasks == 0:   #   全部处理完毕
                    self.stop()
                else:   #   fetcher没有处理完毕
                    if self.crawler_mode == 1:
                        gevent.sleep()
            else:
                pre_depth = url_data.depth
                curr_depth = pre_depth + 1  #   当前深度

                #   生成URL list
                link_generator = HtmlAnalyzer.extract_links(url_data.html, url_data.url, self.crawl_tags)
                link_list = [url for url in link_generator]
                if self.dynamic_parse:  #   WebKit动态解析
                    link_generator = self.webkit.extract_links(url_data.url)
                    link_list.extend([url for url in link_generator])
                link_list = list(set(link_list))    #   去重

                #   遍历解析出的URL list
                for index, link in enumerate(link_list):
                    if not self.check_url_usable(link):
                        continue
                    if curr_depth > self.depth:  # 最大爬行深度判断
                        if self.crawler_stopped.isSet():
                            break
                        else:
                            self.crawler_stopped.set()
                            break

                    if len(self.fetcher_cache) == self.max_url_num:  # 最大收集URL数量判断
                        if self.crawler_stopped.isSet():
                            break
                        else:
                            self.crawler_stopped.set()
                            break
                    link = to_unicode(link)
                    url = UrlData(link, depth=curr_depth)
                    #   此处调整顺序，应该先加入下载队列
                    self.fetcher_queue.put(url, block=True)
                    self.fetcher_cache.insert(url)  #   加入到已经处理fetcher队列

                #   插件部分，暂时不关注
                for plugin_name in self.plugin_handler:  # 循环动态调用初始化时注册的插件
                    try:
                        plugin_obj = eval(plugin_name)()
                        plugin_obj.start(url_data)
                    except Exception, e:
                        import traceback
                        traceback.print_exc()

                self.crawler_queue.task_done()


    def check_url_usable(self, link):
        '''
        检查URL是否符合可用规则
        '''
        if link in self.fetcher_cache:
            return False

        if not link.startswith("http"):
            return False

        if self.same_origin:
            if not self._check_same_origin(link):
                return False

        link_ext = os.path.splitext(urlparse.urlsplit(link).path)[-1][1:]
        if link_ext in self.ignore_ext:
            return False

        return True


    def feed_url(self, url):
        '''
        设置初始爬取URL
        '''

        #   先判断是否为字符串
        #   basestring是str和unicode的基类，也是抽象类，因此不能被调用和实例化，但可以用来判断一个对象是否为str或者unicode的实例
        #   isinstance(obj ,basestring) 等价于 isinstance(obj, (str,  unicode))
        if isinstance(url, basestring): #   判断一个对象是否是一个已知的类型
            url = to_unicode(url)   #   将输入的字符串转化为unicode对象
            url = UrlData(url)  #   URL对象类

        #   同源策略
        #   如果两个URL的协议，域名和端口相同，则表示他们是同源的
        if self.same_origin:
            url_part = urlparse.urlparse(unicode(url))  #   获取urlparse对象
            self.origin = (url_part.scheme, url_part.netloc)    #   scheme为协议，netloc为域名

        #   初始URL加入到HTML下载队列，此时url为UrlData类的对象
        self.fetcher_queue.put(url, block=True)


    def stop(self):
        '''
        终止爬虫
        '''
        self.stopped.set()  #   stopped事件置位


    def _maintain_spider(self):
        '''
        维护爬虫池:
        1)从池中剔除死掉的crawler和fetcher
        2)根据剩余任务数量及池的大小补充crawler和fetcher
        维持爬虫池饱满
        '''

        #   gevent模型
        if self.crawler_mode == 1:
            for greenlet in list(self.crawler_pool):
                if greenlet.dead:
                    self.crawler_pool.discard(greenlet)
            for i in xrange(min(self.crawler_queue.qsize(), self.crawler_pool.free_count())):
                self.crawler_pool.spawn(self.crawler)

        for greenlet in list(self.fetcher_pool):
            if greenlet.dead:
                self.fetcher_pool.discard(greenlet)
        for i in xrange(min(self.fetcher_queue.qsize(), self.fetcher_pool.free_count())):
            fetcher = Fetcher(self)
            self.fetcher_pool.start(fetcher)


    def _check_same_origin(self, current_url):
        '''
        检查两个URL是否同源
        '''
        current_url = to_unicode(current_url)
        url_part = urlparse.urlparse(current_url)   #   获取urlparse对象
        url_origin = (url_part.scheme, url_part.netloc) #   检查协议与域名
        return url_origin == self.origin


if __name__ == '__main__':
    spider = Spider(concurrent_num=20, depth=5, max_url_num=300, crawler_mode=1, dynamic_parse=False)
    url = "http://www.baidu.com"
    spider.feed_url(url)    #   设置初始爬取URL
    spider.start()