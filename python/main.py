"""
ETL for stellar history files.

Script to download xdr files from a s3 bucket,
unpack them, filter the transactions in them by a given asset,
and write the relevant transactions to a database.
"""

import os
import time
import logging

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from botocore.exceptions import ClientError
import psycopg2
from xdrparser import parser

# Get constants from env variables
PYTHON_PASSWORD = os.environ['PYTHON_PASSWORD']
ASSET_CODE = os.environ['ASSET_CODE']
ASSET_TYPE = 'alphaNum' + '4' if len(ASSET_CODE) <= 4 else '12'
ASSET_ISSUER = os.environ['ASSET_ISSUER']
NETWORK_PASSPHARSE = os.environ['NETWORK_PASSPHRASE']
MAX_RETRIES = int(os.environ['MAX_RETRIES'])
BUCKET_NAME = os.environ['BUCKET_NAME']

try:
    CORE_DIRECTORY = os.environ['CORE_DIRECTORY']
except KeyError:
    CORE_DIRECTORY = ''


def setup_s3():
    """Set up the s3 client with anonymous connection."""
    s3 = boto3.client('s3', config=Config(signature_version=UNSIGNED))
    logging.info('Successfully initialized S3 client')
    return s3


def setup_postgres():
    """Set up a connection to the postgres database using the user 'python'."""
    conn = psycopg2.connect("postgresql://python:{}@db:5432/{}".format(PYTHON_PASSWORD, ASSET_CODE.lower()))
    logging.info('Successfully connected to postgres database {}'.format(ASSET_CODE.lower()))
    return conn


def get_last_file_sequence(conn, cur):
    """Get the sequence of the last file scanned."""
    cur.execute('select * from lastfile;')
    conn.commit()
    last_file = cur.fetchone()[0]

    return last_file


def download_file(s3, file_name):
    """Download the ledger-filename and transactions-filename files from the s3 bucket."""
    # File transactions-004c93bf.xdr.gz will be in:
    # BUCKET_NAME/CORE_DIRECTORY/transactions/00/4c/93/

    # "ledger-004c93bf" > "00/4c/93/"
    file_number = file_name.split('-')[1]
    sub_directory = '/'.join(file_number[i:i + 2] for i in range(0, len(file_number), 2))
    sub_directory = sub_directory[:9]

    sub_directory = ('ledger/' if 'ledger' in file_name else 'transactions/') + sub_directory

    for attempt in range(MAX_RETRIES + 1):
        try:
            logging.info('Trying to download file {}.xdr.gz'.format(file_name))
            s3.download_file(BUCKET_NAME, CORE_DIRECTORY + sub_directory + file_name + '.xdr.gz', file_name + '.xdr.gz')
            logging.info('File {} downloaded'.format(file_name))
            break
        except ClientError as e:

            # If you failed to get the file more than MAX_RETRIES times: raise the exception
            if attempt == MAX_RETRIES:
                logging.error('Reached retry limit when downloading file {}, quitting.'.format(file_name))
                raise

            # If I get a 404, it might mean that the file does not exist yet, so I will try again in 3 minutes
            error_code = int(e.response['Error']['Code'])
            if error_code == 404:
                logging.warning('404, could not get file {}, retrying in 3 minutes'.format(file_name))
                time.sleep(180)


def get_ledgers_dictionary(ledgers):
    """Get a dictionary of a ledgerSequence and closing time."""
    return {ledger['header']['ledgerSeq']: ledger['header']['scpValue']['closeTime'] for ledger in ledgers}


def write_to_postgres(conn, cur, transactions, ledgers_dictionary, file_name):
    """Filter payment/trust operations and write them to the database."""
    logging.info('Writing contents of file: {} to database'.format(file_name))
    for transaction_history_entry in transactions:
        timestamp = ledgers_dictionary.get(transaction_history_entry['ledgerSeq'])

        for transaction in transaction_history_entry['txSet']['txs']:
            memo = transaction['tx']['memo']['text']
            tx_hash = transaction['hash']

            for operation in transaction['tx']['operations']:
                # Operation type 1 = Payment
                if operation['body']['type'] == 1:
                    # Check if this is a payment for our asset
                    if operation['body']['paymentOp']['asset'][ASSET_TYPE] is not None and \
                                    operation['body']['paymentOp']['asset'][ASSET_TYPE]['assetCode'] == ASSET_CODE and \
                                    operation['body']['paymentOp']['asset'][ASSET_TYPE]['issuer']['ed25519'] == ASSET_ISSUER:
                        source = transaction['tx']['sourceAccount']['ed25519']
                        destination = operation['body']['paymentOp']['destination']['ed25519']
                        amount = operation['body']['paymentOp']['amount']

                        # Override the tx source with the operation source if it exists
                        try:
                            source = operation['sourceAccount'][0]['ed25519']
                        except (KeyError, IndexError):
                            pass

                        cur.execute("INSERT INTO payments VALUES ('{}','{}',{},{},'{}',to_timestamp({}));".
                                    format(source, destination, amount,
                                           "'" + memo + "'" if memo is not None else 'NULL', tx_hash, timestamp))

                # Operation type 6 = Change Trust
                elif operation['body']['type'] == 6:
                    # Check if this is a trustline for our asset
                    if operation['body']['changeTrustOp']['line'][ASSET_TYPE] is not None and \
                                    operation['body']['changeTrustOp']['line'][ASSET_TYPE]['assetCode'] == ASSET_CODE and \
                                    operation['body']['changeTrustOp']['line'][ASSET_TYPE]['issuer']['ed25519'] == ASSET_ISSUER:
                        source = transaction['tx']['sourceAccount']['ed25519']

                        # Override the tx source with the operation source if it exists
                        try:
                            source = operation['sourceAccount'][0]['ed25519']
                        except (KeyError, IndexError):
                            pass

                        cur.execute("INSERT INTO trustlines VALUES ('{}', {}, '{}',to_timestamp({}));"
                                    .format(source,"'" + memo + "'" if memo is not None else 'NULL', tx_hash, timestamp))

    # Update the 'lastfile' entry in the database
    cur.execute("UPDATE lastfile SET name = '{}'".format(file_name))
    conn.commit()
    logging.info('Successfully wrote contents of file: {} to database'.format(file_name))


def get_new_file_sequence(old_file_name):
    """
    Return the name of the next file to scan.

    Transaction files are stored with an ascending hexadecimal name, for example:
    └── transactions
    └── 00
        └── 72
            ├── 6a
            │   ├── transactions-00726a3f.xdr.gz
            │   ├── transactions-00726a7f.xdr.gz
            │   ├── transactions-00726abf.xdr.gz
            │   └── transactions-00726aff.xdr.gz

    So get the sequence of the last file scanned > convert to decimal > add 64 > convert back to hex >
    remove the '0x' prefix > and add '0' until the file name is 8 letters long
    """
    new_file_name = int(old_file_name, 16)
    new_file_name = new_file_name + 64
    new_file_name = hex(new_file_name)
    new_file_name = new_file_name.replace('0x', '')
    new_file_name = '0' * (8 - len(new_file_name)) + new_file_name

    return new_file_name


def main():
    """Main entry point."""
    # Initialize everything
    logging.basicConfig(level='INFO', format='%(asctime)s | %(levelname)s | %(message)s')
    conn = setup_postgres()
    cur = conn.cursor()
    file_sequence = get_last_file_sequence(conn, cur)
    s3 = setup_s3()

    while True:
        # Download the files from S3
        download_file(s3, 'ledger-' + file_sequence)
        download_file(s3, 'transactions-' + file_sequence)

        # Unpack the files
        ledgers = parser.parse('ledger-{}.xdr.gz'.format(file_sequence))
        transactions = parser.parse('transactions-{}.xdr.gz'.format(file_sequence),
                                    with_hash=True, network_id=NETWORK_PASSPHARSE)

        # Get a ledger:closeTime dictionary
        ledgers_dictionary = get_ledgers_dictionary(ledgers)

        # Remove the files from storage
        logging.info('Removing downloaded files.')
        os.remove('ledger-{}.xdr.gz'.format(file_sequence))
        os.remove('transactions-{}.xdr.gz'.format(file_sequence))

        # Write the data to the postgres database
        write_to_postgres(conn, cur, transactions, ledgers_dictionary, file_sequence)

        # Get the name of the next file I should work on
        file_sequence = get_new_file_sequence(file_sequence)


if __name__ == '__main__':
    main()