import pgeocode
import csv
import io

def load_minnesota_zips(db, ZipCode):
    existing = ZipCode.query.count()
    if existing > 0:
        return existing

    nomi = pgeocode.Nominatim('us')

    mn_zips_added = 0
    for zip_code in range(55001, 56800):
        zip_str = str(zip_code).zfill(5)
        result = nomi.query_postal_code(zip_str)
        if result is not None and hasattr(result, 'latitude'):
            lat = result.latitude
            lon = result.longitude
            state = result.state_code if hasattr(result, 'state_code') else None
            city = result.place_name if hasattr(result, 'place_name') else None

            if lat != lat or lon != lon:
                continue

            if state and state != 'MN':
                continue

            zc = ZipCode(zip=zip_str, city=city, state=state or 'MN', lat=float(lat), lon=float(lon))
            db.session.add(zc)
            mn_zips_added += 1

    wi_ranges = [(53001, 54991)]
    for start, end in wi_ranges:
        for zip_code in range(start, end + 1):
            zip_str = str(zip_code).zfill(5)
            result = nomi.query_postal_code(zip_str)
            if result is not None and hasattr(result, 'latitude'):
                lat = result.latitude
                lon = result.longitude
                state = result.state_code if hasattr(result, 'state_code') else None
                city = result.place_name if hasattr(result, 'place_name') else None

                if lat != lat or lon != lon:
                    continue

                if state and state != 'WI':
                    continue

                zc = ZipCode(zip=zip_str, city=city, state=state or 'WI', lat=float(lat), lon=float(lon))
                db.session.add(zc)
                mn_zips_added += 1

    db.session.commit()
    return mn_zips_added
