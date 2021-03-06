from xml.dom import ValidationErr
from django.shortcuts import render
from rest_framework import routers, serializers, viewsets 
from rest_framework.decorators import api_view
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework_gis.filters import InBBoxFilter
from rest_framework import status
from rest_framework import permissions
from rest_framework.parsers import JSONParser 
from django.shortcuts import render, get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from .models import *
from .serializers import *
import requests
from rest_framework.generics import ListAPIView

import json
import datetime
import os
from uuid import uuid4
from zipfile import ZipFile
import math
import logging
import requests
import shutil


class DatasetViewSet(viewsets.ModelViewSet): #This is datasetviewset , viewset is defined in order to perform curd efficiently and also this will be tightly coupled with the models 
    queryset = Dataset.objects.all()
    serializer_class = DatasetSerializer # connecting serializer

class AOIViewSet(viewsets.ModelViewSet): 
    queryset = AOI.objects.all()
    serializer_class = AOISerializer # connecting serializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['dataset']

class LabelViewSet(viewsets.ModelViewSet): 
    queryset = Label.objects.all()
    serializer_class = LabelSerializer # connecting serializer
    bbox_filter_field = 'geom'
    filter_backends = (InBBoxFilter, DjangoFilterBackend,) # it will take bbox like this api/v1/fetch-label/?in_bbox=-90,29,-89,35
    bbox_filter_include_overlapping = True # Optional
    filterset_fields = ['aoi'] 


class RawdataApiView(APIView):
    def get(self, request,aoi_id, *args, **kwargs):
        pass

    def post(self, request,aoi_id, *args, **kwargs):
        obj = AOI.objects.get(id=aoi_id)
        if not obj:
            return Response(
                {"res": "Object with Dataset id does not exists"},
                status=status.HTTP_400_BAD_REQUEST
            )
        obj.download_status = 0
        obj.save()
        raw_data_params={
            "geometry":json.loads(obj.geom.geojson),
            "filters":{"tags":{"polygon":{"building":[]}}},
            "geometryType":["polygon"]
        }
        result=request_rawdata(raw_data_params)
        file_download_url=result['download_url']
        process_rawdata(file_download_url,aoi_id)
        obj.download_status = 1
        obj.last_fetched_date = datetime.datetime.utcnow()
        obj.save()
        return Response("Success", status=status.HTTP_201_CREATED)


def request_rawdata(request_params):
    """This will make call to galaxy API get the databack and provides response as json

    Args:
        request_params (dict): Galaxy API Request Body

    Raises:
        ImportError: If galaxy url is not exists

    Returns:
        Response(json): API Response
    """
    try : 
        galaxy_url=os.environ.get('GALAXY_URL')
    except:
        raise ImportError(
                "Galaxy URL is not defined on env variable"
        )
    #following block should be a background task and api should deliver response quickly as possible
    headers = {'Content-type': "text/plain; charset=utf-8"}
    print(request_params)
    with requests.post(url = galaxy_url, data = json.dumps(request_params) ,headers=headers) as r : # no curl option , only request for now curl can be implemented when we see it's usage
        response_back = r.json()
        print(response_back)
        return response_back


def process_rawdata(file_download_url,aoi_id):
    """This will create temp directory , Downloads file from URL provided, Unzips it Finds a geojson file , Process it and finally removes proccessed Geojson file and downloaded zip file from Directory"""
    r = requests.get(file_download_url)
            # Check whether the export path exists or not
    path='temp/'
    isExist = os.path.exists(path)
    if not isExist:
        # Create a exports directory because it does not exist
        os.makedirs(path)
    file_temp_path=f"""{path}{str(uuid4())}.zip""" # making unique path each time when response is recieved -- being independent whatever the response is 
    open(file_temp_path, 'wb').write(r.content)
    print("Zip File from API wrote to disk")
    with ZipFile(file_temp_path, 'r') as zipObj:
        # Get a list of all archived file names from the zip
        listOfFileNames = zipObj.namelist()
        # Iterate over the file names
        geojson_file_path=f"""{path}/geojson/"""

        for fileName in listOfFileNames:
            # Check filename endswith csv
            if fileName.endswith('.geojson'):
                if fileName != "clipping_boundary.geojson":
                # Extract a single file from zip
                    zipObj.extract(fileName, geojson_file_path)
                    print(f"""Geojson file{fileName} from API wrote to disk""")
                    break
        geojson_file=f"""{geojson_file_path}{fileName}"""        
        process_geojson(geojson_file,aoi_id)
    remove_file(file_temp_path)
    remove_file(geojson_file)


def remove_file(path: str) -> None:
    """Used for removing temp file
    """
    os.unlink(path)

def process_geojson(geojson_file_path,aoi_id):
    """Responsible for Processing Geojson file from directory , Opens the file reads the record , Checks either record present or not if not inserts into database

    Args:
        geojson_file_path (_type_): _description_
        aoi_id (_type_): _description_

    Raises:
        ValidationErr: _description_
    """
    print("Geojson Processing Started")

    with open(geojson_file_path) as f:
        data = json.load(f)
        for i in range(len(data["features"])):
            properties = data["features"][i]["properties"]
            osm_id=properties["osm_id"]
            geometry=data["features"][i]["geometry"]
            print(osm_id)
            try:
                go = Label.objects.get(osm_id=int(osm_id))
                print("already exists")
            except :
                label = LabelSerializer(data={'osm_id': int(osm_id),"geom":geometry,"aoi":aoi_id})
                if label.is_valid():
                    label.save()
                else:
                    raise ValidationErr(label.errors)
    print("writing to database finished")

DEFAULT_TILE_SIZE=254
DEFAULT_ZOOM_LEVEL=19
   
@api_view(['POST'])   
def image_download_api(request):
    """_summary_

    Args:
        dataset_id: int - id of the dataset
        source : str - source url of OAM if present or any other URL - Optional

    Returns:
        Download status
    """
    print(request.data)

    serializer=ImageDownloadSerializer(data=request.data)
    if serializer.is_valid(raise_exception=True):
        dataset_id=request.data.get('dataset_id')
        try:
            source =request.data.get('source')
        except :
            source = None
    # need to get all the aoi associated with dataset, that's why filtering except getting 
    aois = AOI.objects.filter(dataset=dataset_id)

    base_path=f"training/{dataset_id}" # this is the base path where imagery will be downloaded if not present it will create one 
    if  os.path.exists(base_path):
        shutil.rmtree(base_path)
    os.makedirs(base_path)

   

    # looping through each of them and processing it one by one , later on we can specify each aoi to no of threads available 
    for obj in aois :
        # TODO : Here assign each aoi to different thread as much as possible and available 
        if obj.imagery_status != 0 :
            print(f"Running Download process for aoi : {obj.id} - dataset : {dataset_id}")
            obj.imagery_status = 0
            obj.save()
            # print(obj.geom.coords[0])
            bbox_coords=bbox(obj.geom.coords[0])
            print(f"bbox is : {bbox_coords}")
    
            tile_size = DEFAULT_TILE_SIZE # by default 
            zm_level=DEFAULT_ZOOM_LEVEL
            
            # start point where we will start downloading the tiles

            start_point_lng=bbox_coords[0] #getting the starting lat lng
            start_point_lat=bbox_coords[1]
            
            # end point where we should stop downloading the tile 
            end_point_lng=bbox_coords[2] #getting the ending lat lng
            end_point_lat=bbox_coords[3]
            
            # Note :  lat=y-axis, lng=x-axis 
            # getting tile coordinate for first point of bbox
            start_x,start_y=latlng2tile(zoom=zm_level,lat=start_point_lat,lng=start_point_lng,tile_size=tile_size)
            start=[start_x,start_y]
            
            # getting tile coordinate for last point of bbox
            end_x,end_y=latlng2tile(zoom=zm_level,lat=end_point_lat,lng=end_point_lng,tile_size=tile_size)
            end=[end_x,end_y]
            try:
                # start downloading 
                if source : # if source is supplied from post request itself
                    download_imagery(start,end,zm_level,dataset_id=dataset_id,base_path=base_path,source=source)
                else:
                    download_imagery(start,end,zm_level,dataset_id=dataset_id,base_path=base_path) 
                obj.imagery_status = 1
                # obj.last_fetched_date = datetime.datetime.utcnow()
                obj.save()
                
            except Exception as ex : # if download process is failed somehow then it should be indicated as not downloaded
                print(ex)
                obj.imagery_status = -1
                # obj.last_fetched_date = datetime.datetime.utcnow()
                obj.save()
        else:
            print(f"There is running process already for : {obj.id} - dataset : {dataset_id} , Skippinggg")
    aoi = AOI.objects.filter(dataset=dataset_id).values()
    
    res_serializer=ImageDownloadResponseSerializer(data=list(aoi),many=True) 
    
    aoi_list_queryset = AOI.objects.filter(dataset=dataset_id)
    
    aoi_list=[r.id for r in aoi_list_queryset]

    label= Label.objects.filter(aoi__in=aoi_list).values()
    serialized_field=LabelFileSerializer(data=list(label),many=True)
    try:
        if serialized_field.is_valid(raise_exception=True):
            with open(f"training/{dataset_id}/labels.geojson", 'w', encoding='utf-8') as f: 
                f.write(json.dumps(serialized_field.data))
            f.close()

    except Exception as ex :
        print (ex)
        raise ex
    
    if res_serializer.is_valid(raise_exception=True):
        print(res_serializer.data)
        return Response(res_serializer.data, status=status.HTTP_201_CREATED)
            
def bbox(coord_list):
    """_summary_

    Args:
        coord_list (_type_): Polygon coordinate list

    Returns:
        list: bbox coords 
    
    """
    box = []
    for i in (0,1):
         res = sorted(coord_list, key=lambda x:x[i])
         box.append((res[0][i],res[-1][i]))
    
    ret = [box[0][0],box[1][0], box[0][1],box[1][1]]
    return ret            

def convert2worldcd(lat,lng,tile_size):
    """
    World coordinates  are measured from the Mercator projection's origin (the northwest corner of the map at 180 degrees longitude and approximately 85 degrees latitude) and increase in the x direction towards the east (right) and increase in the y direction towards the south (down). Because the basic Mercator  tile is 256 x 256 pixels, the usable world coordinate space is {0-256}, {0-256}
    """
    siny = math.sin((lat * math.pi) / 180)
    siny = min(max(siny, -0.9999), 0.9999)
    world_x= tile_size * (0.5 + (lng / 360))
    world_y = tile_size * (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi))
    # print("world coordinate space is %s, %s",world_x,world_y)
    return world_x,world_y

def latlng2tile(zoom,lat,lng,tile_size):  
    """By dividing the pixel coordinates by the tile size and taking the integer parts of the result, you produce as a by-product the tile coordinate at the current zoom level."""
    zoom_byte=1 << zoom #converting zoom level to pixel bytes 
    # print(zoom_byte)
    w_x,w_y=convert2worldcd(lat,lng,tile_size)
    t_x=math.floor((w_x * zoom_byte) / tile_size)
    t_y=math.floor((w_y * zoom_byte) / tile_size)
    return t_x,t_y

def download_imagery(start : list , end :list , zm_level , dataset_id ,base_path, source='maxar'):
    """Downloads imagery from start to end tile coordinate system

    Args:
        start (list):[tile_x,tile_y]
        end (list): [tile_x,tile_y],
        source (string): it should be eithre url string or maxar value
        zm_level : Zoom level
        dataset_id (int) : Dataset id 

    """
    
    begin_x=start[0] # this will be the beginning of the download loop for x 
    begin_y=start[1] # this will be the beginning of the download loop for x
    stop_x=end[0] # this will be the end of the download loop for x
    stop_y=end[1]# this will be the end of the download loop for x
    
    print(f"Download starting from {start} to {end}")
    
    start_x=begin_x # starting loop from beginning 
    start_y=begin_y # starting y loop from beginnig
    source_name="OAM" #default
    
    while start_x <= stop_x: # download for x section while keeping y as constant 
        start_y=begin_y
        while start_y >= stop_y: # download for y section while keeping x as constant 
            download_path= [start_x,start_y]                
            if source == 'maxar': 
                try : 
                    connect_id=os.environ.get('MAXAR_CONNECT_ID')
                except:
                    raise ImportError(
                            "Connect id for maxar is not supplied"
                    )
                source_name=source
                download_url=f"https://services.digitalglobe.com/earthservice/tmsaccess/tms/1.0.0/DigitalGlobe:ImageryTileService@EPSG:3857@jpg/{zm_level}/{download_path[0]}/{download_path[1]}.jpg?connectId={connect_id}&flipy=true"
            
            #add multiple logic on supported sources here 
            else:
                # source should be url as string , like this :  https://tiles.openaerialmap.org/62dbd947d8499800053796ec/0/62dbd947d8499800053796ed/{z}/{x}/{y}
                download_url=source.format(x=download_path[0],y=download_path[1],z=zm_level)
            file = f"{base_path}/{source_name}-{start_x}-{start_y}-{zm_level}.png"
            if os.path.exists(file):
                os.remove(file)

            with open(file, 'wb') as handle:
                response = requests.get(download_url, stream=True)

                if not response.ok:
                    print(response)

                for block in response.iter_content(1024):
                    if not block:
                        break
                    handle.write(block)
            print(f"Downloaded : {download_path}")
            start_y=start_y-1 # decrease the y 
        
        start_x=start_x+1 # increase the x 

    #TODO: Save geojson labels to the same folder
       
        

    
