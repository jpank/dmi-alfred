#! /usr/bin/python

import sys, getopt, webbrowser

def main(argv):
    postcode=''
    try:
        opts, args =getopt.getopt(argv, "hi:o:", ["ifile", "ofile"])
    except getopt.GetoptError:
        print 'dmi.py -i <query>'
        sys.ext(2)
    postcode= args[0]
    # print args[0]
    
    dmi = 'http://www.dmi.dk/vejr/til-lands/byvejr/by/vis/DK/%s' % (postcode)
    webbrowser.open_new(dmi)
    
if __name__ == "__main__":
   main(sys.argv[1:])