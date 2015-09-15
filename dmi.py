import webbrowser
query="{query}"

# city identification
if query == 'aarhus':
    postcose = 8000
elif query == 'aalborg':
    postcode = 9000
elif query == 'sonderborg':
    postcode = 6400
else: postcode = query


dmi = 'http://www.dmi.dk/vejr/til-lands/byvejr/by/vis/DK/%s' % (postcode)
webbrowser.open_new(dmi)


