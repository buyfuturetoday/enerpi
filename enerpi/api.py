# -*- coding: utf-8 -*-
# noinspection PyUnresolvedReferences
from enerpi.base import CONFIG, TZ, DATA_PATH, FILE_LOGGING, LOGGING_LEVEL
# noinspection PyUnresolvedReferences
from enerpi.database import init_catalog, get_ts_last_save, extract_log_file, HDF_STORE
from enerpi.enerpimeter import receiver_msg_generator, msg_to_dict, TS_DATA_MS, RMS_ROLL_WINDOW_SEC, DELTA_SEC_DATA


def enerpi_receiver_generator():
    """
    Generator of broadcasted values by ENERPI Logger.

    It can be used by any machine in the same network as the ENERPI Logger. It decrypts the encrypted broadcast and
    returns a dict of vars - values.
    Used by the webserver for read & stream real-time values.

    :return: :dict:
    """
    gen = receiver_msg_generator(False)
    count = 0
    while True:
        try:
            msg, _t1, _t2 = next(gen)
            yield msg_to_dict(msg)
            count += 1
        except StopIteration:
            print('StopIteration in it {}'.format(count))
            return None


def enerpi_default_config():
    """
    Default configuration for ENERPI Data Catalog, read from INI file.

    :return: :dict: parameters
    """
    conf = {'store': HDF_STORE,
            'DATA_PATH': DATA_PATH,
            'delta': DELTA_SEC_DATA,
            'window': RMS_ROLL_WINDOW_SEC,
            'ts': TS_DATA_MS,
            'LOGGING_LEVEL': LOGGING_LEVEL,
            'FILE_LOGGING': FILE_LOGGING}
    return conf


def enerpi_data_catalog(check_integrity=False, **kwargs):
    """
    Get ENERPI data catalog for access & operation.

    :param check_integrity: :bool: False by default. If true, checks integrity and generates / updates data index.
    :param kwargs: :dict:
    :return: :EnerpiCatalog:
    """
    return init_catalog(check_integrity=check_integrity, **kwargs)


# def get_last_saved_data(path_st=HDF_STORE, get_last_sample=True, verbose=True, n=10):
#     return get_ts_last_save(path_st, get_last_sample=get_last_sample, verbose=verbose, n=n)


# def enerpi_log(log_file=FILE_LOGGING, extract_temps=False, verbose=True):
#     return extract_log_file(log_file, extract_temps=extract_temps, verbose=verbose)


if __name__ == '__main__':
    from time import time
    import pandas as pd
    from prettyprinting import *
    # from enerpi.base import timeit

    # @timeit('_get_all_data', verbose=True)
    # def _get_all_data(catalog, async_get, with_summary_data=True):
    #     return catalog.get(start=catalog.min_ts, with_summary=with_summary_data, async_get=async_get)

    pd.set_option('display.width', 200)
    tic = time()
    cat = enerpi_data_catalog(check_integrity=False)
    toc_cat = time()
    print_magenta('CATALOG TOOK: {:.3f} s'.format(toc_cat - tic))

    # data, data_s = _get_all_data(cat, True, with_summary_data=True)
    # toc_get_a = time()
    # print_magenta('TOOK:\n\tGET ALL ASYNC: {:.3f} s ({} rows)'.format(toc_get_a - toc_cat, len(data)))
    #
    # data_2, data_s2 = _get_all_data(cat, False, with_summary_data=True)
    # toc_get = time()
    # print_magenta('TOOK:\n\tGET ALL NO ASYNC: {:.3f} s ({} rows)'.format(toc_get - toc_get_a, len(data_2)))
    #
    # print_info(data.tail(3))
    # print_info(data_2.tail(3))
    #
    # print_cyan(data_s.tail(3))
    # print_cyan(data_s2.tail(3))
    # print_red(pd.DataFrame(data == data_2).all().all())
    # print_red(pd.DataFrame(data_s == data_s2).all().all())
