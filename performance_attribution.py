#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Jan  9 16:50:11 2017

@author: lishiwang
"""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pandas import Series, DataFrame, Panel
from datetime import datetime
import os
import statsmodels.api as sm

from data import data
from strategy_data import strategy_data
from position import position
from barra_base import barra_base


# 业绩归因类，对策略中的股票收益率（注意：并非策略收益率）进行归因

class performance_attribution(object):
    """This is the class for performance attribution analysis.

    foo
    """

    def __init__(self, input_position, *, mv='default', benchmark_position='default', stock_returns='default',
                 portfolio_returns='default'):
        self.pa_data = strategy_data()
        # 市值数据可传入或自动读取
        if mv == 'default':
            self.pa_data.stock_price = data.read_data(['MarketValue'], ['MarketValue'])
        else:
            self.pa_data.stock_price = pd.Panel({'MarketValue': mv})
        self.pa_position = input_position

        # 如果传入基准持仓数据，则归因超额收益
        self.is_benchmark = False
        if benchmark_position != 'default':
            self.pa_position.holding_matrix = self.pa_position.holding_matrix.sub(benchmark_position.holding_matrix,
                                                                                  fill_value=0)
            self.is_benchmark = True

        # 如果没有传入股票收益数据，则读入股票价格自己计算
        if stock_returns == 'default':
            temp_closeprice = data.read_data(['ClosePrice_adj'], ['ClosePrice_adj'])
            self.pa_data.stock_price['ClosePrice_adj'] = temp_closeprice.ix['ClosePrice_adj']
            self.stock_returns = np.log(self.pa_data.stock_price.ix['ClosePrice_adj'].div(
                self.pa_data.stock_price.ix['ClosePrice_adj'].shift(1)))
        else:
            self.stock_returns = stock_returns

        # 如果有传入组合收益，则直接用这个组合收益，如果没有则自己计算
        self.port_returns = pd.DataFrame()
        self.is_port_ret_imported = False
        if portfolio_returns != 'default':
            self.port_returns = portfolio_returns
            self.is_port_ret_imported = True

        self.pa_returns = pd.DataFrame()
        self.port_expo = pd.DataFrame()
        self.port_pa_returns = pd.DataFrame()
        self.style_factor_returns = pd.Series()
        self.industry_factor_returns = pd.Series()
        self.country_factor_return = pd.Series()
        self.residual_returns = pd.Series()
        # 业绩归因为基于barra因子的业绩归因
        self.bb = barra_base()

        self.discarded_stocks_num = pd.DataFrame()
        self.discarded_stocks_wgt = pd.DataFrame()
        
    # 进行业绩归因
    # 用discard_factor可以定制用来归因的因子，将不需要的因子的名字或序号以list写入即可
    # 注意，只能用来删除风格因子，不能用来删除行业因子或country factor
    def execute_performance_attribution(self, *, discard_factor=[]):
        # 建立barra因子库
        self.bb.construct_barra_base()
        # 将被删除的风格因子的暴露全部设置为0
        self.bb.bb_data.factor_expo.ix[discard_factor, :, :] = 0
        # 再次将不能交易的值设置为nan
        self.bb.bb_data.discard_untradable_data()
        # 建立储存因子收益的dataframe
        self.pa_returns = pd.DataFrame(0, index=self.bb.bb_data.factor_expo.major_axis, 
                                       columns = self.bb.bb_data.factor_expo.items)
        # 计算barra base因子的因子收益
        self.bb.get_bb_factor_return()
        # barra base因子的因子收益即是归因的因子收益
        self.pa_returns = self.bb.bb_factor_return

    # 将归因的结果进行整理
    def analyze_pa_outcome(self):
        # 首先根据持仓比例计算组合在各个因子上的暴露
        self.port_expo = np.einsum('ijk,jk->ji', self.bb.bb_data.factor_expo.fillna(0), self.pa_position.holding_matrix)

        # 根据因子收益和因子暴露计算组合在因子上的收益
        self.port_pa_returns = self.pa_returns.mul(self.port_expo)

        # 计算各类因子的总收益情况
        # 风格因子收益
        self.style_factor_returns = self.port_pa_returns.ix[:, 0:10].sum(1)
        # 行业因子收益
        self.industry_factor_returns = self.port_pa_returns.ix[:, 10:38].sum(1)
        # 国家因子收益
        self.country_factor_return = self.port_pa_returns.ix[:, 38]

        # 如果需要，则直接根据股票收益算出组合的收益
        if not self.is_port_ret_imported:
            self.port_returns = self.pa_position.holding_matrix.mul(self.stock_returns, fill_value=0)

        # 残余收益，即alpha收益，为组合收益减去之前那些因子的收益
        # 注意下面会提到，缺失数据会使得残余收益变大
        self.residual_returns = self.port_returns - (self.style_factor_returns+self.industry_factor_returns+
                                                     self.country_factor_return)

    # 处理那些没有归因的股票，即有些股票被策略选入，但因没有因子暴露值，而无法纳入归因的股票
    # 此dataframe处理这些股票，储存每期这些股票的个数，以及它们在策略中的持仓权重
    # 注意，此类股票的出现必然导致归因的不准确，因为它们归入到了组合总收益中，但不会被归入到缺少暴露值的因子收益中，因此进入到残余收益中
    # 这样不仅会使得残余收益含入因子收益，而且使得残余收益与因子收益之间具有显著相关性
    # 如果这样暴露缺失的股票比例很大，则使得归因不具有参考价值
    def handle_discarded_stocks(self):
        self.discarded_stocks_num = self.pa_returns.mul(0)
        self.discarded_stocks_wgt = self.pa_returns.mul(0)
        # 因子暴露有缺失值，没有参与归因的股票
        if_discarded = self.bb.bb_data.factor_expo.isnull()
        # 没有参与归因，同时还持有了
        discarded_and_held = if_discarded.mul(self.pa_position.holding_matrix, axis='items').astype(bool)
        # 各个因子没有参与归因的股票个数与持仓比例
        self.discarded_stocks_num = discarded_and_held.sum(2)
        # 注意：如果有benchmark传入，则持仓为负数，这时为了反应绝对量，持仓比例要取绝对值
        self.discarded_stocks_wgt = discarded_and_held.mul(self.pa_position.holding_matrix, axis='items').abs().sum(2)
        # 计算总数
        self.discarded_stocks_num['total'] = self.discarded_stocks_num.sum(1)
        self.discarded_stocks_wgt['total'] = self.discarded_stocks_wgt.sum(1)

        # 循环输出警告
        for time, temp_data in self.discarded_stocks_num.iterrows():
            # 一旦没有归因的股票数超过总持股数的10%，或其权重超过10%，则输出警告
            if temp_data.ix['total'] >= 0.1*((self.pa_position.holding_matrix.ix[time] != 0).sum()) or \
            self.discarded_stocks_wgt.ix[time, 'total'] >= 0.1:
                print('At time: {0}, the number of stocks(*discarded times) held but discarded in performance attribution '
                      'is: {1}, the weight of these stocks(*discarded times) is: {2}.\nThus the outcome of performance '
                      'attribution at this time can be significantly distorted. Please check discarded_stocks_num and '
                      'discarded_stocks_wgt for more information.\n'.format(time, temp_data.ix['total'],
                                                                            self.discarded_stocks_wgt.ix[time, 'total']))

    # 进行画图
    def plot_performance_attribution(self):

        # 第一张图分解组合的累计收益来源
        f1 = plt.figure()
        ax1 = f1.add_subplot(1,1,1)
        plt.plot(self.style_factor_returns.cumsum()*100, label='style')
        plt.plot(self.industry_factor_returns.cumsum()*100, label='industry')
        plt.plot(self.country_factor_return.cumsum()*100, label='country')
        plt.plot(self.residual_returns.cumsum()*100, label='residual')
        ax1.set_xlabel('Time')
        ax1.set_ylabel('Cumulative Log Return (%)')
        ax1.set_title('The Cumulative Log Return of Factor Groups')
        ax1.legend(loc='best')

        # 第二张图分解组合的累计风格收益
        f2 = plt.figure()
        ax2 = f2.add_subplot(1,1,1)
        (self.port_pa_returns.ix[:, 0:10].cumsum(0)*100).plot()
        ax2.set_xlabel('Time')
        ax2.set_ylabel('Cumulative Log Return (%)')
        ax2.set_title('The Cumulative Log Return of Style Factors')
        ax2.legend(loc='best')

        # 第三张图分解组合的累计行业收益
        f3 = plt.figure()
        ax3 = f3.add_subplot(1, 1, 1)
        (self.port_pa_returns.ix[:, 10:38].cumsum(0) * 100).plot()
        ax3.set_xlabel('Time')
        ax3.set_ylabel('Cumulative Log Return (%)')
        ax3.set_title('The Cumulative Log Return of Industrial Factors')
        ax3.legend(loc='best')

        # 第四张图画组合的累计风格暴露
        f4 = plt.figure()
        ax4 = f4.add_subplot(1, 1, 1)
        self.port_expo.ix[:, 0:10].cumsum(0).plot()
        ax4.set_xlabel('Time')
        ax4.set_ylabel('Cumulative Factor Exposures')
        ax4.set_title('The Cumulative Style Factor Exposures of the Portfolio')
        ax4.legend(loc='best')

        # 第五张图画组合的累计行业暴露
        f5 = plt.figure()
        ax5 = f5.add_subplot(1, 1, 1)
        self.port_expo.ix[:, 10:38].cumsum(0).plot()
        ax5.set_xlabel('Time')
        ax5.set_ylabel('Cumulative Factor Exposures')
        ax5.set_title('The Cumulative Industrial Factor Exposures of the Portfolio')
        ax5.legend(loc='best')


























































































































































































































