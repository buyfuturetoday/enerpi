# -*- coding: utf-8 -*-
import asyncio
import concurrent.futures as futures
import glob
import json
import logging
import numpy as np
import os
import pandas as pd
import re
import shutil
from time import time


'''
DATA_PATH
    YEAR_XXXX
        MONTH_XX.h5     data_processed, data_summary, extra_summary
        MONTH_XY.h5     data_processed, data_summary, extra_summary
        ...
    YEAR_XXXX
        MONTH_XY.h5     data_processed, data_summary, extra_summary
        ...
    CURRENT_MONTH
        DAY_XX.h5       data_processed, data_summary
        DAY_XX.h5       data_processed, data_summary
        ...
    TODAY
        TODAY.h5        data_processed
    TEMP_DATA.h5        data_raw

    OLD_STORES
        BKP_OLD_1
        ...

* Reconstrucción de índice:
    Lee todos los store, los copia a BACKUP_ORIG si procede, crea estructura de árbol de archivo,
     distribuye data y guarda stores en árbol. Borra originales.
    * Recontrucción parcial: ajusta sólo los nuevos stores creados (para ejecutar al inicio). Sin backup
* Lectura de datos:
    De t_0 a t_f(=now). Genera paths de stores del intervalo requerido, las lee en async, las concatena y devuelve.
    Si se lee con t_f > TODAY, se genera data_processed y/o data_summary
* Archivado periódico:
    0. Cada hora, Acumulación de TEMP_DATA en TODAY:
        lee RAW TEMP_DATA, procesa, append nuevos datos a TODAY --> Devuelve señal de borrado de TEMP_DATA.
            **process_data**
    1. Al cierre del día:
        lee TODAY, GENERA SUMMARY, escribe DAY_XX, Limpia TODAY.
            **process_data_summary**
    2. Al cierre de mes:
        lee todos los DAY_XX de CURRENT_MONTH, concatena, GENERA EXTRA SUMMARY, escribe MONTH_XX, Borra stores leídos.
            **process_data_summary_extra**
    3. (OPC). Cierre Anual.
'''


ARCHIVE_AUTO = 0
ARCHIVE_HOURLY = 1
ARCHIVE_DAILY = 2
ARCHIVE_MONTHLY = 3

STORE_EXT = '.h5'
KWARGS_SAVE = dict(complevel=9, complib='blosc', fletcher32=True)

DIR_CURRENT_MONTH = 'CURRENT_MONTH'
DIR_BACKUP = 'OLD_STORES'
ST_TODAY = os.path.join(DIR_CURRENT_MONTH, 'TODAY' + STORE_EXT)
INDEX = 'data_catalog.csv'

YEAR_MASK = '{}_YEAR_{:%Y}'
RG_YEAR_MASK = re.compile('(?P<name>\.*)_YEAR_(?P<year>\d{4})')
MONTH_MASK = '{}_{:%Y_MONTH_%m}' + STORE_EXT
RG_MONTH_MASK = re.compile('(?P<name>\.*)_(?P<year>\d{4})_MONTH_(?P<month>\d{2})')
DAY_MASK = '{}_{:%Y_%m_DAY_%d}' + STORE_EXT
RG_DAY_MASK = re.compile('(?P<name>\.*)_(?P<year>\d{4})_(?P<month>\d{2})_DAY_(?P<day>\d{2})')


def timeit(cadena_log, verbose=False, *args_dec):
    def real_deco(function):
        def wrapper(*args, **kwargs):
            kwargs_print = {}
            for k in kwargs.keys():
                if k.startswith('print_'):
                    kwargs_print[k] = kwargs.pop(k)
            tic = time()
            out = function(*args, **kwargs)
            took = time() - tic
            if verbose:
                print('***TIMEIT ' + cadena_log.format(*args_dec, **kwargs_print) + ' TOOK: {:.3f} s'.format(took))
            logging.debug('TIMEIT ' + cadena_log.format(*args_dec, **kwargs_print) + ' TOOK: {:.3f} s'.format(took))
            return out
        return wrapper
    return real_deco


class HDFTimeSeriesCatalog(object):

    def __init__(self,
                 base_path=os.getcwd(),
                 preffix='DATA',
                 raw_file='temp' + STORE_EXT,
                 key_raw_data='/raw_data',
                 key_summary_data='/hours',
                 key_summary_extra='/days',
                 catalog_file=INDEX,
                 check_integrity=True,
                 verbose=True,
                 backup_original=True):
        self.base_path = os.path.abspath(base_path)
        self.name = preffix
        # TODO Usar
        self.verbose = verbose

        self.raw_store = raw_file + STORE_EXT if not raw_file.endswith(STORE_EXT) else raw_file
        self.key_raw = key_raw_data
        self.key_summary = key_summary_data
        self.key_summary_extra = key_summary_extra

        # TODO Usar
        self.backup_orig = backup_original

        # Index:
        self.catalog_file = catalog_file
        self.tree = self.get_index(check_index=check_integrity)
        self.min_ts = self.tree['ts_ini'].min() if self._exist() else np.nan
        self.index_ts = self._ts_filepath(self.catalog_file)

        if check_integrity:
            self.archive_periodic(new_data=None, reload_index=False)
        # if self.verbose:
        #     print_ok(self.tree)

    @staticmethod
    def is_raw_data(data):
        # Is RAW Data? If it is, call process_data before storing it
        # Implementar en subclase
        raise NotImplementedError

    @staticmethod
    def process_data(data):
        # From RAW Data to processed data
        # Implementar en subclase
        raise NotImplementedError

    @staticmethod
    def process_data_summary(data):
        # From Processed data to Processed data + summary
        # Implementar en subclase
        # return data, data_s
        raise NotImplementedError

    @staticmethod
    def process_data_summary_extra(data):
        # From Processed data to Processed data + summary + summary extra
        # Implementar en subclase
        # return data, data_s, data_se
        raise NotImplementedError

    def _exist(self):
        return self.tree is not None and not self.tree.empty

    def __repr__(self):
        mints = '{:%B/%Y}'.format(self.min_ts) if self._exist() else '--'
        idxts = '{:%d/%m/%y %H:%M:%S}'.format(self.index_ts) if self._exist() else '--'
        cad = '<HDFCatalog[{}] ->{}; From:{}; last_upd={}>\n\tINDEX [{}]:\n{}\n'
        catalog = self.tree.sort_values(by=['is_cat', 'key', 'ts_fin']) if self._exist() else '\t\t*NO INDEX*'
        return cad.format(self.name, self.base_path, mints, idxts, self.catalog_file, catalog)

    def _ts_filepath(self, rel_path):
        if self.base_path not in rel_path:
            p = os.path.join(self.base_path, rel_path)
        else:
            p = rel_path
        return pd.Timestamp.fromtimestamp(os.path.getmtime(p)) if os.path.exists(p) else None

    def _load_index(self):
        p = os.path.join(self.base_path, self.catalog_file)
        try:
            index = pd.read_csv(p, index_col=0, parse_dates=['ts_ini', 'ts_fin', 'ts_st'])
            index.cols = index.cols.map(lambda x: json.loads(x.replace("'", '"')))
            return index.drop_duplicates(subset=['st', 'key', 'ts_st', 'ts_ini', 'ts_fin'])
        except OSError:
            logging.error('Error leyendo INDEX en "{}". Se reconstruye.'.format(p))
            return None

    def _save_index(self, index):
        # print_secc('Salvando INDEX')
        p = os.path.join(self.base_path, self.catalog_file)
        try:
            index.to_csv(p)
            return True
        except Exception as e:
            logging.error('ERROR en _save_index "{}"; [{}]'.format(p, e.__class__))
            return False

    def _check_index(self, index):
        # print_secc('CHECK INDEX')
        index = index.copy()
        paths = index['st'].apply(lambda x: os.path.join(self.base_path, x))
        times = [self._ts_filepath(p) for p in paths]
        pb_bkp = os.path.join(self.base_path, DIR_BACKUP)
        new_stores = [f.replace(self.base_path + os.path.sep, '')
                      for f in glob.glob(os.path.join(self.base_path, '**'), recursive=True)
                      if (f.endswith(STORE_EXT) and (pb_bkp not in f) and
                          (f != self.raw_store) and (f not in paths.values))]
        index['new_ts'] = times
        index['modif'] = index['ts_st'] != index['new_ts']
        if new_stores:
            logging.warning('NEW STORES WERE FOUND: {}'.format(new_stores))
        new_stores += paths.loc[index['new_ts'].notnull() & index['modif']].drop_duplicates().tolist()
        cond_no_change = index['new_ts'].notnull() & ~index['modif']
        index = index[cond_no_change].drop(['modif', 'ts_st'], axis=1).rename(columns={'new_ts': 'ts_st'})
        lost_stores = index.loc[index['ts_st'].isnull(), 'st'].drop_duplicates().tolist()
        if new_stores:
            logging.debug('THERE ARE MODIFIED STORES. INDEX WILL BE RE-CREATED FOR THESE: {}'.format(new_stores))
            new_rows = self._make_index(distribute_existent=True, paths=new_stores)
            index = index.set_index('st').drop(new_rows['st'].drop_duplicates().values, errors='ignore').reset_index()
            index = pd.DataFrame(pd.concat([index, new_rows], axis=0)).sort_values(by='ts_ini')
        if lost_stores:
            logging.warning('SOME STORES WERE LOST. THESE WILL BE REMOVED FROM INDEX: {}'.format(lost_stores))
            index = index.set_index('st').drop(lost_stores, errors='ignore').sort_values(by='ts_ini').reset_index()
        self._save_index(index)
        # if new_stores or lost_stores:
        self.min_ts = index['ts_ini'].min() if not index.empty else np.nan
        self.index_ts = self._ts_filepath(self.catalog_file)
        return index

    def _load_hdf(self, rel_path, key=None, func_store=None):
        p = os.path.join(self.base_path, rel_path)
        k = key or self.key_raw
        try:
            # tic = time()
            with pd.HDFStore(p, mode='r') as st:
                if func_store is None:
                    data = st[k]
                else:
                    data = func_store(st)
            # print('load_hdf {} in {:.3f}'.format(p, time() - tic))
            return data
        except KeyError as e:
            logging.error('load_hdf KEYERROR -> ST:"{}", KEY:{}; -> {}'.format(p, k, e))
            with pd.HDFStore(p, mode='r') as st:
                logging.error(st)
            return None
        except OSError as e:
            logging.debug('load_hdf OSERROR -> ST:"{}", KEY:"{}"'.format(p, k, e, e.__class__, os.path.exists(p)))
        except Exception as e:
            logging.error('load_hdf ERROR -> ST:"{}", KEY:"{}"; -> "{}" ["{}"]. exist?: {}'
                          .format(p, k, e, e.__class__, os.path.exists(p)))
            return None

    def _save_hdf(self, data, path, key, mode='a', **kwargs):
        p = os.path.join(self.base_path, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with pd.HDFStore(p, mode=mode, **kwargs) as st:
            try:
                if (type(key) is str) and (type(data) is pd.DataFrame):
                    st.append(key, data)
                else:
                    for k, df in zip(key, data):
                        st.append(k, df)
            except ValueError as e:
                logging.error('ERROR en _save_hdf: {}'.format(e))
                logging.debug(key, type(key) is str), (type(data) is pd.DataFrame)
                logging.debug(st.keys())
                assert()
        logging.debug('STORE "{}", "{}"\t->\t{:.1f} KB'.format(path, key, os.path.getsize(p) / 1000))
        return True

    def _make_index_path(self, ts, w_day=False):
        if ts.date() == pd.Timestamp.today().date():
            p = ST_TODAY
        elif w_day:
            p = os.path.join(DIR_CURRENT_MONTH, DAY_MASK.format(self.name, ts))
        else:
            p = os.path.join(YEAR_MASK.format(self.name, ts), MONTH_MASK.format(self.name, ts))
        return p

    def _get_paths_interval(self, ts_ini, ts_fin=None):
        ahora = pd.Timestamp.now()
        ts_ini = pd.Timestamp(ts_ini)
        ts_fin = pd.Timestamp(ts_fin) if ts_fin else ahora
        periods = (ts_fin.year * 12 + ts_fin.month) - (ts_ini.year * 12 + ts_ini.month)
        index = pd.DatetimeIndex(freq='M', start=ts_ini.date(), periods=periods + 1)
        paths = []
        for i in index:
            if (ahora.year == i.year) and (ahora.month == i.month):
                start = ahora.replace(day=1).date() if len(paths) > 0 else ts_ini.date()
                index_d = pd.DatetimeIndex(freq='D', start=start, periods=ts_fin.day - start.day + 1)
                [paths.append(self._make_index_path(i, w_day=True)) for i in index_d]
            else:
                paths.append(self._make_index_path(i, w_day=False))
        return paths

    def _load_today(self):
        return self._load_hdf(ST_TODAY, key=self.key_raw)

    def _load_current_month(self, with_summary_data=True):
        days_cm = [p.replace(self.base_path + os.path.sep, '')
                   for p in glob.glob(os.path.join(self.base_path, DIR_CURRENT_MONTH, '*{}'.format(STORE_EXT)))]
        if with_summary_data:
            extracted = [self._load_hdf(p, func_store=lambda st: (st[self.key_raw], st[self.key_summary]))
                         for p in days_cm]
            df = pd.DataFrame(pd.concat([e[0] for e in extracted if e is not None], axis=0)).sort_index()
            df_s = pd.DataFrame(pd.concat([e[1] for e in extracted if e is not None], axis=0)).sort_index()
            return df, df_s, days_cm
        else:
            df = pd.DataFrame(pd.concat([self._load_hdf(p, key=self.key_raw) for p in days_cm], axis=0)).sort_index()
            idx = df.index
            # print_red('Current month data stats:\n- {} rows, from {:%c} to {:%c}, index: unique={}, monotonic={}'
            #           .format(df.shape[0], idx[0], idx[-1], idx.is_unique, idx.is_monotonic_increasing))
            logging.debug('Current month data stats:\n- {} rows, from {:%c} to {:%c}, index: unique={}, monotonic={}'
                          .format(df.shape[0], idx[0], idx[-1], idx.is_unique, idx.is_monotonic_increasing))
            return df, days_cm

    def _classify_data(self, df):
        paths_dfs_dfssum = []
        ahora = pd.Timestamp.now()
        # ts_ini, ts_fin = df.index[0], df.index[-1]
        gb_años = df.groupby(pd.TimeGrouper(freq='A'))
        for ts_year, d_year in gb_años:
            if not d_year.empty:
                gb_meses = d_year.groupby(pd.TimeGrouper(freq='M'))
                for ts_month, d_month in gb_meses:
                    if not d_month.empty:
                        if (ts_year.year == ahora.year) and (ts_month.month == ahora.month):
                            # CURRENT MONTH
                            # print_red(DIR_CURRENT_MONTH)
                            gb_dias = d_month.groupby(pd.TimeGrouper(freq='D', closed='left', label='left'))
                            for ts_day, d_day in gb_dias:
                                if not d_day.empty:
                                    if ts_day.day == ahora.day:
                                        # TODAY
                                        logging.debug(ST_TODAY + '\n{}\n{}'.format(d_day.dtypes, d_day.head()))
                                        paths_dfs_dfssum.append((ST_TODAY, d_day, None, None))
                                    else:
                                        # ARCHIVE DAY
                                        p = self._make_index_path(ts_day, w_day=True)
                                        logging.debug('# ARCHIVE DAY {:%Y-%m-%d} -> {}'.format(ts_day, p))
                                        d_day_p, c_day = self.process_data_summary(d_day)
                                        paths_dfs_dfssum.append((p, d_day_p, c_day, None))
                        else:
                            # ARCHIVE MONTH
                            p = self._make_index_path(ts_month, w_day=False)
                            logging.debug('# ARCHIVE MONTH --> {}'.format(p))
                            d_month_p, c_month, c_month_extra = self.process_data_summary_extra(d_month)
                            paths_dfs_dfssum.append((p, d_month_p, c_month, c_month_extra))
        return sorted(paths_dfs_dfssum, key=lambda x: x[0])

    def _is_catalog_path(self, st, ts_ini, ts_fin):
        if st == ST_TODAY:
            return True
        elif st == self.raw_store:
            return False
        paths = self._get_paths_interval(ts_ini, ts_fin)
        if (len(paths) == 1) and (paths[0] == st):
            return True
        return False

    def _gen_index_entries(self, paths=None):
        dataframes = []
        pb_bkp = os.path.join(self.base_path, DIR_BACKUP)
        if paths is None:
            paths = glob.glob(os.path.join(self.base_path, '**'), recursive=True)
            logging.debug(paths)
        for f in paths:
            if f.endswith(STORE_EXT) and (pb_bkp not in f) and (f != self.raw_store):
                relat_path = f.replace(self.base_path + os.path.sep, '')
                st_mtime = self._ts_filepath(relat_path)
                new = self._load_hdf(f, self.key_raw,
                                     func_store=lambda st: [(relat_path, k, st[k].index[0], st[k].index[-1], st_mtime,
                                                             len(st[k]), k == self.key_raw, list(st[k].columns))
                                                            for k in st.keys()])
                if new:
                    dataframes += new
        df = pd.DataFrame(dataframes, columns=['st', 'key', 'ts_ini', 'ts_fin', 'ts_st', 'n_rows', 'is_raw', 'cols']
                          ).sort_values(by='ts_ini')
        if not df.empty:
            claves = df[df.is_raw].groupby('st').first()
            iscat = pd.DataFrame(claves.apply(lambda x: self._is_catalog_path(x.name, x['ts_ini'], x['ts_fin']),
                                              axis=1).rename('is_cat'))
            if not iscat.empty:
                return df.set_index('st').join(iscat).fillna(False).sort_values(by='ts_ini').reset_index()
            df['is_cat'] = False
            return df.sort_values(by='ts_ini')
        return df.T.append(pd.Series([], name='ts_cat').T).T

    @timeit('_make_index')
    def _make_index(self, distribute_existent=True, paths=None):
        df = self._gen_index_entries(paths=paths)
        if distribute_existent and not df.empty:
            if not df[df.is_raw & ~df.is_cat].empty:
                raw_to_distr = df[df.is_raw & ~df.is_cat]
                logging.debug('Distribuyendo datos desde:\n{}'.format(raw_to_distr))
                data = pd.DataFrame(pd.concat([self._load_hdf(p, key=self.key_raw)
                                               for p in raw_to_distr['st']])).sort_index()
                if self.is_raw_data(data):
                    data = self.process_data(data)
                mod_paths = self.distribute_data(data, mode='a')
                for p in raw_to_distr['st']:
                    p_bkp = os.path.join(self.base_path, DIR_BACKUP, p)
                    os.makedirs(os.path.dirname(p_bkp), exist_ok=True)
                    shutil.copyfile(os.path.join(self.base_path, p), p_bkp)
                    os.remove(os.path.join(self.base_path, p))
                logging.debug('2º PASADA con modpaths')
                df = df.set_index('st').drop(mod_paths, errors='ignore').reset_index()
                df_2 = self._gen_index_entries(paths=mod_paths)
                df = pd.concat([df.drop(raw_to_distr.index, errors='ignore'), df_2], axis=0)
            else:
                logging.debug('No hay stores que distribuir')
            return df
        elif not df.empty:
            return df
        return None

    def load_store(self, path_idx, process_data=False, with_summary=False):

        def _get_data_from_store(st):
            d1 = st[self.key_raw]
            try:
                d2 = st[self.key_summary]
            except KeyError:
                d1_bis, d2 = self.process_data_summary(d1)
            return d1, d2

        asyncio.sleep(0)
        if with_summary:
            extracted = list(self._load_hdf(path_idx, func_store=_get_data_from_store))
        else:
            extracted = list(self._load_hdf(path_idx, key=self.key_raw))
        asyncio.sleep(0)
        if process_data and with_summary:
            extracted[1] = self.process_data(extracted[0])
        elif process_data:
            extracted = self.process_data(extracted)
        # asyncio.sleep(0)
        return extracted

    @timeit('archive_periodic')
    def archive_periodic(self, new_data=None, reload_index=False):
        """
        * Archivado periódico:
            0. Cada hora, Acumulación de TEMP_DATA en TODAY:
                lee TEMP_DATA, procesa, append nuevos datos a TODAY --> Devuelve señal de borrado de TEMP_DATA.
                    **process_data**
            1. Al cierre del día:
                lee TODAY, GENERA SUMMARY, escribe DAY_XX, Limpia TODAY.
                    **process_data_summary**
            2. Al cierre de mes:
                lee todos los DAY_XX de CURRENT_MONTH, concatena, GENERA EXTRA SUMMARY, escribe MONTH_XX, y borra DAY's.
                    **process_data_summary_extra**
            3. (OPC). Cierre Anual.

        :param new_data:
        :param reload_index:
        """

        if reload_index:
            self.tree = self.get_index(check_index=False)
        mod_paths = []
        ahora = pd.Timestamp.now()
        month_now = ahora.year * 12 + ahora.month
        monthly_archive = False
        data_current_month = self.tree[self.tree['st'].str.contains(DIR_CURRENT_MONTH)] if self._exist() else None
        if data_current_month is not None and not data_current_month.empty:
            # Archivo mensual
            month_ts = data_current_month['ts_ini'].min()
            month_cm = month_ts.year * 12 + month_ts.month
            if not data_current_month.empty and (month_cm < month_now):
                month, days_cm = self._load_current_month(with_summary_data=False)
                logging.info('** ARCHIVE MONTH: {}'.format(days_cm))
                mod_paths += self.distribute_data(month, mode='a')
                self.tree = self.tree.set_index('st').drop(days_cm, errors='ignore').reset_index()
                for p in days_cm:
                    remove = False
                    if ST_TODAY == p:
                        if ST_TODAY not in mod_paths:
                            remove = True
                    elif RG_DAY_MASK.search(p).groupdict()['month'] != ahora.month:
                            remove = True
                    if remove:
                        p_bkp = os.path.join(self.base_path, DIR_BACKUP, p)
                        os.makedirs(os.path.dirname(p_bkp), exist_ok=True)
                        shutil.copyfile(os.path.join(self.base_path, p), p_bkp)
                        os.remove(os.path.join(self.base_path, p))
                monthly_archive = len(mod_paths) > 0

        ts_today = self._ts_filepath(ST_TODAY)
        if not monthly_archive and ts_today:
            day_now = ahora.toordinal()
            try:
                today_min = self.tree.set_index('st').loc[ST_TODAY, 'ts_ini'].toordinal()
            except AttributeError:
                logging.error(self.tree)
                today_min = self.tree.set_index('st').loc[ST_TODAY, 'ts_ini'].ix[0].toordinal()
            if (today_min < day_now) or (ts_today.toordinal() < day_now):
                logging.debug('ARCHIVE DAY')
                today = self._load_today()
                mod_paths += self.distribute_data(today, mode='a')
                self.tree = self.tree.set_index('st').drop(ST_TODAY, errors='ignore').reset_index()
                if ST_TODAY not in mod_paths:
                    p_bkp = os.path.join(self.base_path, DIR_BACKUP, ST_TODAY)
                    os.makedirs(os.path.dirname(p_bkp), exist_ok=True)
                    shutil.copyfile(os.path.join(self.base_path, ST_TODAY), p_bkp)
                    os.remove(os.path.join(self.base_path, ST_TODAY))

        if new_data is not None:
            logging.debug('ARCHIVE NEW RAW DATA: {}'.format(new_data.shape))
            if self.is_raw_data(new_data):
                mod_paths += self.distribute_data(self.process_data(new_data), mode='a')
            else:
                mod_paths += self.distribute_data(new_data, mode='a')

        if mod_paths:
            mod_paths = list(set(mod_paths))
            logging.debug('Modificando index en archive_periodic: mod_paths: {}'.format(mod_paths))
            index = self.tree.copy()
            new_rows = self._make_index(distribute_existent=False, paths=mod_paths)
            index = index.set_index('st').drop(new_rows['st'].drop_duplicates().values, errors='ignore').reset_index()
            index = pd.DataFrame(pd.concat([index, new_rows], axis=0)).sort_values(by='ts_ini')
            self.tree = index
            self._save_index(index)
            self.min_ts = self.tree['ts_ini'].min() if not self.tree.empty else np.nan
            self.index_ts = self._ts_filepath(self.catalog_file)

    @timeit('update_catalog')
    def update_catalog(self, data=None):
        """
        Execute this function periodically, within the raw data generator process, to maintain the catalog updated.
        This function reads the temporal data store, adds it to the catalog, and deletes (recreates) the temporal store.
        """
        temp_data = data if data is not None else self._load_hdf(self.raw_store, self.key_raw)
        self.archive_periodic(new_data=temp_data, reload_index=True)
        p = os.path.join(self.base_path, self.raw_store)
        with pd.HDFStore(p, 'w'):
            info = 'Temporal data has been archived. Reset of "{}" is done. Store new size: {:.1f} KB'
            logging.debug(info.format(p, os.path.getsize(p) / 1000))
            # print_red(info.format(p, os.path.getsize(p) / 1000))

    def get_index(self, check_index=True):
        """
        Loading and checking of the catalog index (It's contained in a CSV file in the catalog root directory).
        ** The index is updated or rebuilt if necessary.
        :return: index, Pandas DataFrame with the catalog of all included HDF Stores.
        """

        index = self._load_index()
        if index is None:
            index = self._make_index(distribute_existent=True)
            if index is not None and not index.empty:
                self._save_index(index)
        elif check_index:
            index = self._check_index(index)
        return index

    @timeit('_distribute_data')
    def distribute_data(self, data, mode='a'):
        paths_dfs_dfssum = self._classify_data(data)
        mod_paths = []
        for p, d1, d2, d3 in paths_dfs_dfssum:
            mod_paths.append(p)
            f = list(filter(lambda x: x[0] is not None, zip([d1, d2, d3],
                                                            [self.key_raw, self.key_summary, self.key_summary_extra])))
            dfs, keys = list(zip(*f))[0], list(zip(*f))[1]
            p_abs = os.path.join(self.base_path, p)
            if mode == 'a' and os.path.exists(p_abs):  # 1º se lee, se concatena, y se eliminan duplicados
                # print('** Leyendo información previa del STORE: {}'.format(p_abs))
                old_dfs = self._load_hdf(p, func_store=lambda st: [st[k] for k in keys])
                dfs = [pd.DataFrame(pd.concat([old, df], axis=0)
                                    ).sort_index().reset_index().drop_duplicates(subset='ts').set_index('ts')
                       for old, df in zip(old_dfs, dfs)]
            self._save_hdf(dfs, p, keys, mode='w', **KWARGS_SAVE)
        return mod_paths

    def get(self, start=None, end=None, last_hours=None, with_summary=False, async_get=True):

        def _concat_loaded_data(dfs, ini, fin=None):
            try:
                dataframe = pd.DataFrame(pd.concat([df for df in dfs if df is not None], axis=0)).sort_index()
                if fin is not None:
                    return dataframe.loc[ini:fin]
                return dataframe.loc[ini:]
            except ValueError as e:
                logging.error('GET DATA ERROR: {}'.format(e))
                return None

        # tic = time()
        if last_hours is not None:
            start = pd.Timestamp.now().replace(minute=0, second=0, microsecond=0) - pd.Timedelta(hours=last_hours)
            paths_idx = self._get_paths_interval(ts_ini=start)
        else:
            if start is None:
                start = self.min_ts
            paths_idx = self._get_paths_interval(ts_ini=start, ts_fin=end)
        # toc_paths = time()
        # print('** paths_idx:', len(paths_idx), '; PATHS_TOOK {:.3f} s'.format(toc_paths - tic))
        process_data_paths = [False] * len(paths_idx)
        load_last_raw_data = (last_hours is not None) or ((end is not None) and
                                                          (self.tree.ts_fin.max() >= pd.Timestamp(end)))
        if load_last_raw_data:
            paths_idx.append(self.raw_store)
            process_data_paths.append(True)
        if async_get and len(paths_idx) > 3:
            with futures.ProcessPoolExecutor(max_workers=min(4, len(paths_idx))) as executor:
                future_loads = {executor.submit(self.load_store, p,
                                                process_data=w_proc, with_summary=with_summary): p
                                for p, w_proc in zip(paths_idx, process_data_paths)}
                extracted = [future.result() for future in futures.as_completed(future_loads)]

        else:
            extracted = [self.load_store(p, process_data=w_proc, with_summary=with_summary)
                         for p, w_proc in zip(paths_idx, process_data_paths)]
        # toc_get = time()
        if with_summary:
            data = _concat_loaded_data(list(zip(*extracted))[0], start, end)
            data_s = _concat_loaded_data(list(zip(*extracted))[1], start, end)
            return data, data_s
        return _concat_loaded_data(extracted, start, end)

    @timeit('_get_all_data', verbose=True)
    def get_all_data(self, with_summary_data=True, async_get=True):
        return self.get(start=self.min_ts, with_summary=with_summary_data, async_get=async_get)

    # def info_catalog(self):
    #     # TODO Tabla de información del catálogo: ruta, archivo, n_rows, ts_ini, ts_fin, medidas de completitud
    #     raise NotImplementedError
    #
    # def backup(self, path_backup, compact_data=None):
    #     # TODO backup a ruta alternativa, con compresión y opción de replicado de tree o compactado (x años, o total)
    #     raise NotImplementedError
    #
    def export(self):  # , export_to='csv'):
        # TODO append to mysql?
        all_data, all_data_s = self.get_all_data(True, True)
        # if export_to == 'csv':
        all_data.to_csv(os.path.join(self.base_path, 'enerpi_all_data.csv'))
        all_data_s.to_csv(os.path.join(self.base_path, 'enerpi_all_data_summary.csv'))