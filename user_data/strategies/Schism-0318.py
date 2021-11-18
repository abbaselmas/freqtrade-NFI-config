import numpy as np
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
import arrow
from freqtrade.strategy.interface import IStrategy
from freqtrade.strategy import merge_informative_pair
from typing import Dict, List, Optional, Tuple
from pandas import DataFrame, Series
from functools import reduce
from datetime import datetime
from freqtrade.persistence import Trade
from technical.indicators import RMI
from statistics import mean
from cachetools import TTLCache


"""
TODO: 
    - Continue to hunt for a better all around buy signal.
        - Existing signal to "buy the dip" seems to work well but would it be nice to have an additional
          buy signal for strong upward trends without dips?
        - Prevent buys when potential for strong downward trend and not just a dip?
            - Initial 24h range thing seems to be step 1 of this process, anything else we can do?
            - Fibonacci Retracement for resistance/support levels?
    - Tweak ROI Ride
        - Maybe use free_slots as a factor in how eager we are to sell?
    - Tweak sell signal
        - Continue to evaluate good circumstances to sell vs hold
    - Figure out a way to directly feed a daily hyperopt output into a running strategy and reload it?
"""

class Schism_0318(IStrategy):
    """
    Strategy Configuration Items
    """
    timeframe = '5m'
    inf_timeframe = '1h'

    buy_params = {
        'inf-pct-adr': 0.81329,
        'inf-rsi': 11,
        'inf-stake-rmi': 7,
        'mp': 65,
        'rmi-fast': 32,
        'rmi-slow': 22,
        'tf-fiat-rsi': 84,
        'tf-stake-rsi': 29
    }

    sell_params = {}

    minimal_roi = {
        "0": 0.10,
        "15": 0.05,
        "30": 0.025,
        "60": 0.01,
        "120": 0.005,
        "1440": 0
    }

    dynamic_roi = {
        'dynamic_roi_enabled': False,
        'dynamic_roi_type': 'connect'
    }

    stoploss = -0.20

    # Probably don't change these
    use_sell_signal = False
    sell_profit_only = False
    ignore_roi_if_buy_signal = True

    startup_candle_count: int = 72

    # Custom Dicts for storing trade data and other custom things this strategy does
    custom_trade_info = {}
    custom_fiat = "USD"
    custom_current_price_cache: TTLCache = TTLCache(maxsize=100, ttl=300) # 5 minutes
    
    """
    Informative Pair Definitions
    """
    def informative_pairs(self):
        # add existing pairs from whitelist on the inf_timeframe
        pairs = self.dp.current_whitelist()
        informative_pairs = [(pair, self.inf_timeframe) for pair in pairs]
        
        # add additional informative pairs based on certain stakes
        if self.config['stake_currency'] in ('BTC', 'ETH'):
            for pair in pairs:
                # add in the COIN/FIAT pairs (e.g. XLM/USD) on base timeframe
                coin, stake = pair.split('/')
                coin_fiat = f"{coin}/{self.custom_fiat}"
                informative_pairs += [(coin_fiat, self.timeframe)]

            # add in the STAKE/FIAT pair (e.g. BTC/USD) on base and inf timeframes
            stake_fiat = f"{self.config['stake_currency']}/{self.custom_fiat}"
            informative_pairs += [(stake_fiat, self.timeframe)]
            informative_pairs += [(stake_fiat, self.inf_timeframe)]

        return informative_pairs

    """
    Indicator Definitions
    """ 
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Populate/update the trade data if there is any, set trades to false if not live/dry
        self.custom_trade_info[metadata['pair']] = self.populate_trades(metadata['pair'])
    
        # Set up primary indicators
        dataframe['rmi-slow'] = RMI(dataframe, length=21, mom=5)
        dataframe['rmi-fast'] = RMI(dataframe, length=8, mom=4)

        # Momentum Pinball
        dataframe['roc'] = ta.ROC(dataframe, timeperiod=6)
        dataframe['mp']  = ta.RSI(dataframe['roc'], timeperiod=6)

        # Trend Calculations    
        dataframe['rmi-up'] = np.where(dataframe['rmi-slow'] >= dataframe['rmi-slow'].shift(),1,0)      
        dataframe['rmi-dn'] = np.where(dataframe['rmi-slow'] <= dataframe['rmi-slow'].shift(),1,0)      
        dataframe['rmi-up-trend'] = np.where(dataframe['rmi-up'].rolling(3, min_periods=1).sum() >= 2,1,0)      
        dataframe['rmi-dn-trend'] = np.where(dataframe['rmi-dn'].rolling(3, min_periods=1).sum() >= 2,1,0)

        # Informative for STAKE/FIAT and COIN/FIAT on default timeframe, only relevant if stake currency is BTC or ETH
        if self.config['stake_currency'] in ('BTC', 'ETH'):
            coin, stake = metadata['pair'].split('/')
            fiat = self.custom_fiat
            coin_fiat = f"{coin}/{fiat}"
            stake_fiat = f"{stake}/{fiat}"

            # COIN/FIAT (e.g. XLM/USD) - timeframe
            coin_fiat_tf = self.dp.get_pair_dataframe(pair=coin_fiat, timeframe=self.timeframe)
            dataframe[f"{fiat}_rsi"] = ta.RSI(coin_fiat_tf, timeperiod=14)

            # STAKE/FIAT (e.g. BTC/USD) - inf_timeframe
            stake_fiat_tf = self.dp.get_pair_dataframe(pair=stake_fiat, timeframe=self.timeframe)
            stake_fiat_inf_tf = self.dp.get_pair_dataframe(pair=stake_fiat, timeframe=self.inf_timeframe)

            dataframe[f"{stake}_rsi"] = ta.RSI(stake_fiat_tf, timeperiod=14)
            dataframe[f"{stake}_rmi_{self.inf_timeframe}"] = RMI(stake_fiat_inf_tf, length=21, mom=5)

        # Informative indicators for current pair on inf_timeframe
        informative = self.dp.get_pair_dataframe(pair=metadata['pair'], timeframe=self.inf_timeframe)
        informative['rsi'] = ta.RSI(informative, timeperiod=14)
        informative['1d_high'] = informative['close'].rolling(24).max()
        informative['3d_low'] = informative['close'].rolling(72).min()
        informative['adr'] = informative['1d_high'] - informative['3d_low']

        dataframe = merge_informative_pair(dataframe, informative, self.timeframe, self.inf_timeframe, ffill=True)

        return dataframe

    """
    Buy Trigger Signals
    """
    def populate_buy_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        params = self.buy_params
        trade_data = self.custom_trade_info[metadata['pair']]
        conditions = []

        # Persist a buy signal for existing trades to make use of ignore_roi_if_buy_signal = True
        # when this buy signal is not present a sell can happen according to the defined ROI table
        if trade_data['active_trade']:
            # peak_profit factor f(x)=1-x/400, rmi 30 -> 0.925, rmi 80 -> 0.80
            profit_factor = (1 - (dataframe['rmi-slow'].iloc[-1] / 400))
            # grow from 30 -> 70 after 720 minutes starting after 180 minutes
            rmi_grow = self.linear_growth(30, 70, 180, 720, trade_data['open_minutes'])

            conditions.append(dataframe['rmi-up-trend'] == 1)
            conditions.append(trade_data['current_profit'] > (trade_data['peak_profit'] * profit_factor))
            conditions.append(dataframe['rmi-slow'] >= rmi_grow)

        # Normal buy triggers that apply to new trades we want to enter
        else:
            # Primary buy triggers
            conditions.append(
                # "buy the dip" based buy signal using momentum pinball and downward RMI
                # informative timeframe conditions
                (dataframe[f"rsi_{self.inf_timeframe}"] >= params['inf-rsi']) &
                (dataframe['close'] <= dataframe[f"3d_low_{self.inf_timeframe}"] + (params['inf-pct-adr'] * dataframe[f"adr_{self.inf_timeframe}"])) &
                # default timeframe conditions
                (dataframe['rmi-dn-trend'] == 1) &
                (dataframe['rmi-slow'] >= params['rmi-slow']) &
                (dataframe['rmi-fast'] <= params['rmi-fast']) &
                (dataframe['mp'] <= params['mp'])
            )

            # If the stake is BTC or ETH apply additional conditions
            if self.config['stake_currency'] in ('BTC', 'ETH'):
                # default timeframe conditions
                conditions.append(
                    (dataframe[f"{self.config['stake_currency']}_rsi"] <= params['tf-stake-rsi']) | 
                    (dataframe[f"{self.custom_fiat}_rsi"] >= params['tf-fiat-rsi'])
                )
                # informative timeframe conditions
                conditions.append(dataframe[f"{self.config['stake_currency']}_rmi_{self.inf_timeframe}"] <= params['inf-stake-rmi'])

        # Anything below here applies to persisting and new buy signal
        conditions.append(dataframe['volume'].gt(0))

        if conditions:
            dataframe.loc[
                reduce(lambda x, y: x & y, conditions),
                'buy'] = 1

        return dataframe

    """
    Sell Trigger Signals
    """
    def populate_sell_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        params = self.sell_params
        trade_data = self.custom_trade_info[metadata['pair']]
        conditions = []

        # In this strategy all sells for profit happen according to ROI
        # This sell signal is designed only as a "dynamic stoploss"

        # if we are in an active trade for this pair
        if trade_data['active_trade']:     
            # grow from -0.03 -> 0 after 300 minutes starting immediately
            loss_cutoff = self.linear_growth(-0.03, 0, 0, 300, trade_data['open_minutes'])

            # if we are at a loss, consider what the trend looks and preempt the stoploss
            conditions.append(
                (trade_data['current_profit'] < loss_cutoff) & 
                (trade_data['current_profit'] > self.stoploss) &  
                (dataframe['rmi-dn-trend'] == 1) &
                (dataframe['volume'].gt(0))
            )
            # if the peak profit was positive at some point but never reached ROI, set a higher cross point for exit
            if trade_data['peak_profit'] > 0:
                conditions.append(dataframe['rmi-slow'] < 50)
            # if the trade was always negative, the bounce we expected didn't happen
            else:
                conditions.append(dataframe['rmi-slow'] < 10)

            # if there are other open trades in addition to this one, consider the average profit 
            # across them all and how many free slots we have in our sell decision
            if trade_data['other_trades']:
                if trade_data['free_slots'] > 0:
                    # more free slots, the higher this threshold gets
                    hold_pct = (trade_data['free_slots'] / 100) * -1
                    conditions.append(trade_data['avg_other_profit'] >= hold_pct)
                else:
                    # if were out of slots, allow the biggest losing trade to sell regardless of avg profit
                    conditions.append(trade_data['biggest_loser'] == True)

        # the bot comes through this loop even when there isn't an open trade to sell
        # so we pass an impossible condiiton here because we don't want a sell signal 
        # clogging up the charts and not having one leads the bot to crash
        else:
            conditions.append(dataframe['volume'].lt(0))
                           
        if conditions:
            dataframe.loc[
                reduce(lambda x, y: x & y, conditions),
                'sell'] = 1
        
        return dataframe

    """
    Super Legit Custom Methods
    """
    # Populate trades_data from the database
    def populate_trades(self, pair: str) -> dict:
        # Initialize the trades dict if it doesn't exist, persist it otherwise
        if not pair in self.custom_trade_info:
            self.custom_trade_info[pair] = {}

        # init the temp dicts and set the trade stuff to false
        trade_data = {}
        trade_data['active_trade'] = trade_data['other_trades'] = trade_data['biggest_loser'] = False
        self.custom_trade_info['meta'] = {}

        # active trade stuff only works in live and dry, not backtest
        if self.config['runmode'].value in ('live', 'dry_run'):
            
            # find out if we have an open trade for this pair
            active_trade = Trade.get_trades([Trade.pair == pair, Trade.is_open.is_(True),]).all()

            # if so, get some information
            if active_trade:
                # get current price and update the min/max rate
                current_rate = self.get_current_price(pair, True)
                active_trade[0].adjust_min_max_rates(current_rate)

                # get how long the trade has been open in minutes and candles
                present = arrow.utcnow()
                trade_start  = arrow.get(active_trade[0].open_date)
                open_minutes = (present - trade_start).total_seconds() // 60  # floor

                # set up the things we use in the strategy
                trade_data['active_trade']   = True
                trade_data['current_profit'] = active_trade[0].calc_profit_ratio(current_rate)
                trade_data['peak_profit']    = max(0, active_trade[0].calc_profit_ratio(active_trade[0].max_rate))
                trade_data['open_minutes']   : int = open_minutes
                trade_data['open_candles']   : int = (open_minutes // active_trade[0].timeframe) # floor
            else: 
                trade_data['current_profit'] = trade_data['peak_profit']  = 0.0
                trade_data['open_minutes']   = trade_data['open_candles'] = 0

            # if there are open trades not including the current pair, get some information
            # future reference, for *all* open trades: open_trades = Trade.get_open_trades()
            other_trades = Trade.get_trades([Trade.pair != pair, Trade.is_open.is_(True),]).all()

            if other_trades:
                trade_data['other_trades'] = True
                other_profit = tuple(trade.calc_profit_ratio(self.get_current_price(trade.pair, False)) for trade in other_trades)
                trade_data['avg_other_profit'] = mean(other_profit) 
                # find which of our trades is the biggest loser
                if trade_data['current_profit'] < min(other_profit):
                    trade_data['biggest_loser'] = True
            else:
                trade_data['avg_other_profit'] = 0

            # get the number of free trade slots, storing in every pairs dict due to laziness
            open_trades = len(Trade.get_open_trades())
            trade_data['free_slots'] = max(0, self.config['max_open_trades'] - open_trades)

        return trade_data

    # Get the current price from the exchange (or cache)
    def get_current_price(self, pair: str, refresh: bool) -> float:
        if not refresh:
            rate = self.custom_current_price_cache.get(pair)
            # Check if cache has been invalidated
            if rate:
                return rate

        ask_strategy = self.config.get('ask_strategy', {})
        if ask_strategy.get('use_order_book', False):
            ob = self.dp.orderbook(pair, 1)
            rate = ob[f"{ask_strategy['price_side']}s"][0][0]
        else:
            ticker = self.dp.ticker(pair)
            rate = ticker['last']

        self.custom_current_price_cache[pair] = rate
        return rate

    # linear growth, starts at X and grows to Y after A minutes (starting after B miniutes)
    # f(t) = X + (rate * t), where rate = (Y - X) / (A - B)
    def linear_growth(self, start: float, end: float, start_time: int, end_time: int, trade_time: int) -> float:
        time = max(0, trade_time - start_time)
        rate = (end - start) / (end_time - start_time)
        return min(end, start + (rate * trade_time))

    """
    Price protection on trade entry and timeouts, built-in Freqtrade functionality
    https://www.freqtrade.io/en/latest/strategy-advanced/
    """
    def check_buy_timeout(self, pair: str, trade: Trade, order: dict, **kwargs) -> bool:
        bid_strategy = self.config.get('bid_strategy', {})
        ob = self.dp.orderbook(pair, 1)
        current_price = ob[f"{bid_strategy['price_side']}s"][0][0]
        # Cancel buy order if price is more than 1% above the order.
        if current_price > order['price'] * 1.01:
            return True
        return False

    def check_sell_timeout(self, pair: str, trade: Trade, order: dict, **kwargs) -> bool:
        ask_strategy = self.config.get('ask_strategy', {})
        ob = self.dp.orderbook(pair, 1)
        current_price = ob[f"{ask_strategy['price_side']}s"][0][0]
        # Cancel sell order if price is more than 1% below the order.
        if current_price < order['price'] * 0.99:
            return True
        return False

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float, time_in_force: str, **kwargs) -> bool:
        bid_strategy = self.config.get('bid_strategy', {})
        ob = self.dp.orderbook(pair, 1)
        current_price = ob[f"{bid_strategy['price_side']}s"][0][0]
        # Cancel buy order if price is more than 1% above the order.
        if current_price > rate * 1.01:
            return False
        return True

    """
    Override for default Freqtrade ROI table functionality
    """
    def min_roi_reached_entry(self, trade_dur: int) -> Tuple[Optional[int], Optional[float]]:
        dynamic_roi = self.dynamic_roi
        minimal_roi = self.minimal_roi
        
        # if the dynamic_roi dict is defined and enabled, use it, otherwise fallback to default functionality
        if dynamic_roi and dynamic_roi['dynamic_roi_enabled']:
            # linear decay: f(t) = start - (rate * t)
            if dynamic_roi['dynamic_roi_type'] == 'linear':
                start, end = dynamic_roi['dynamic_roi_start'], dynamic_roi['dynamic_roi_end']
                rate = (start - end) / dynamic_roi['dynamic_roi_time']
                min_roi = max(end, start - (rate * trade_dur))
            # exponential decay: f(t) = start * e^(-rate*t)
            elif dynamic_roi['dynamic_roi_type'] == 'exponential':
                start, end = dynamic_roi['dynamic_roi_start'], dynamic_roi['dynamic_roi_end']
                min_roi = max(end, start * np.exp(-dynamic_roi['dynamic_roi_rate']*trade_dur))
            elif dynamic_roi['dynamic_roi_type'] == 'connect':
                # connect the points in the defined table with lines
                past_roi = list(filter(lambda x: x <= trade_dur, minimal_roi.keys()))
                next_roi = list(filter(lambda x: x >  trade_dur, minimal_roi.keys()))
                if not past_roi:
                    return None, None
                current_entry = max(past_roi)
                if not next_roi:
                    return current_entry, minimal_roi[current_entry]
                # use the slope-intercept formula between the two points in the roi table we are between
                else:
                    next_entry = min(next_roi)
                    # y = mx + b
                    x1, y1 = current_entry, minimal_roi[current_entry]
                    x2, y2 = next_entry, minimal_roi[next_entry]
                    m = (y1-y2)/(x1-x2)
                    b = (x1*y2 - x2*y1)/(x1-x2)
                    min_roi = (m * trade_dur) + b
            else:
                min_roi = 0

            return trade_dur, min_roi
        # Default ROI table functionality
        else:
            # Get highest entry in ROI dict where key <= trade-duration
            roi_list = list(filter(lambda x: x <= trade_dur, minimal_roi.keys()))
            if not roi_list:
                return None, None
            roi_entry = max(roi_list)
            return roi_entry, minimal_roi[roi_entry]

