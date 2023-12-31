import cv2
import matplotlib.pyplot as plt
import numpy as np
import os
from dotenv import load_dotenv
import json
import time
import uuid
import requests
import io
import base64
from PIL import Image, ImageFile
from similarity import find_same_string, jaccard_similarity, Levenshtein_similarity
import re

ImageFile.LOAD_TRUNCATED_IMAGES = True

load_dotenv()

class Guide:
    def __init__(self, img, box_info, situation='board'):
        self.img = img
        self.box_info = box_info
        self.situation = situation # 1.길찾기  2.화장실찾기  3.나가기
        
        self.ocr_key = os.getenv("OCR_KEY")
        self.ocr_url = os.getenv("OCR_URL")
        
        self.guide_board = list()
        self.arrow = list()
        self.text = list()
        self.label_onboarding = {0:'guide board',
                                 1:'왼쪽',
                                 2:'오른쪽',
                                 3:'직진',
                                 4:'아래 방향'}
        
        self.label_toilet = {0:'왼쪽',
                             1:'오른쪽',
                             2:'직진',
                             3:'화장실',
                             4:'화장실 입구',
                             5:'아래 방향'            
                             }
        
    def get_text_center(self,box,feat): 
        
        if feat == 'ocr':
            x_coords = [vertex['x'] for vertex in box]
            y_coords = [vertex['y'] for vertex in box]

            center_x = sum(x_coords) / len(x_coords)
            center_y = sum(y_coords) / len(y_coords)
            
        ### --- xyxy xywh 확인 필요 --- ###
        elif feat == 'yolo':
            x1, y1, x2, y2 = box
            center_x = (x1 + x2) / 2
            center_y = (y1 + y2) / 2
            # x, y , w, h = box
            # center_x = x + w//2
            # center_y = y + h//2
        
        return center_x, center_y    
    
    def is_all_english(self, text):
        # 영어만 있으면 true    
        return all(char.isalpha() and ord(char) < 128 for char in text)

    def is_all_korean(self,text):
        # 한글만 있으면 True
        for char in text:
            if not ('\uAC00' <= char <= '\uD7A3'):
                return False
        return True

    def to_byte_img(self, img):
        # ocr 보내려면 바이트 형태의 이미지 값을 넘겨야 해서 변환.
        numpy_image = np.array(img)
        opencv_image = cv2.cvtColor(numpy_image, cv2.COLOR_RGB2BGR)
        image_bytes = cv2.imencode('.jpg', opencv_image)[1].tobytes()

        return image_bytes
    
    def process_ocr_data(self, data):
        
        processed_data = []
        uid = data['images'][0]['uid']
        # json 형태가 좀 복잡함. 아래의 iteration은 한 박스에 대한 정보가 들어있음.
        for box in data['images'][0]['fields']:
            
            infer_text = box['inferText']

            if self.situation == 'board':
                # 한글 외에 다른 문자 있으면 거르기
                if not self.is_all_korean(infer_text):
                    continue
            
            # 중심 좌표 계산
            bounding_poly = box['boundingPoly']['vertices']
            center_x, center_y = self.get_text_center(bounding_poly, 'ocr')

            processed_field = {'uid': uid, 'center': (center_x, center_y), 'text': infer_text}
            processed_data.append(processed_field)

        '''
        [{'uid': 'd83c37b54b~',
        'center': (532.5, 28.25),
        'text': '고속터미널'}, ...]
        '''
        
        return processed_data


    def ocr(self, img):
        
        start = time.time()
        
        request_json = {
            'images': [
                {
                    'format': 'jpg',
                    'name': 'demo'
                }
            ],
            'requestId': str(uuid.uuid4()),
            'version': 'V2',
            'timestamp': int(round(time.time() * 1000))
        }
        
        payload = {'message': json.dumps(request_json).encode('UTF-8')}
        
        byte_img = self.to_byte_img(img)
        files = [('file',byte_img)]
        headers = {
        'X-OCR-SECRET': self.ocr_key
        }

        response = requests.request("POST", self.ocr_url, headers=headers, data = payload, files = files)

        result = response.text.encode('utf8')
        result_json = json.loads(result.decode('utf8').replace("'", '"'))
        
        end = time.time()
        delay = end - start
        print(f"[OCR] 완료! 걸린 응답 시간: {delay}")
        print(result_json)
        processed_data = self.process_ocr_data(result_json)
        
        return processed_data
    
    def crop_boxes(self, img, gb_box, ar_box):
        # 안드로이드에서 base64로 인코딩된 이미지 정보를 가져오게됨.
        decoded_data = base64.b64decode(img)
        image = Image.open(io.BytesIO(decoded_data))
        g_x1, g_y1, g_x2, g_y2 = gb_box['box']
        cropped_image = image.crop((g_x1, g_y1, g_x2, g_y2))
        
        # crop한 이미지에 맞게 화살표의 박스 좌표도 변환을 해준다.
        for box in ar_box:
            x1, y1, x2, y2 = box['box']
            x1, y1, x2, y2 = x1 - g_x1, y1 - g_y1, x2 - g_x1, y2 - g_y1
            box['box'] = [x1, y1, x2, y2]
            
        print(f'[CROP] 변환 완료')
        
        return cropped_image, ar_box
    
    def box_size(self, box):
        ### --- box 좌표의 형태가 xyxy인지 xywh인지 확인 후 수정 필요 --- ###
        x1, y1, x2, y2 = box
        width = x2 - x1
        height = y2 - y1
        return width * height
    
    def distance(self, box1, box2):
        # 박스 중심 좌표 두개 사이의 거리.
        distance = ((box2[0] - box1[0])**2 + (box2[1] - box1[1])**2) ** 0.5
        return distance
    
    def filtering_object(self,box_info):
        
        guide_board = []
        arrows = []
        toilet = []
        outtlier = []
        
        ### --- 박스 정보에 따라 인덱싱 수정해야함 --- ###
        for box in box_info:
            
            if box['label'] == 0: # guide board
                guide_board.append(box)
            elif box['label'] in [1,2,3,4]: # L, R, S, under    
                arrows.append(box)
            elif box['label'] == 8:
                toilet.append(box)
            else:
                outtlier.append(box)
                print(f"[Label 분류]{box['label']} 발견")
        
        try:
            # 안내판이 여러개 검출되었다면 가장 큰 녀석만 골라서 사용할 계획. -> 잘린 안내판이 있을 수 있기에 수정 필요.
            board = guide_board[0]
            
            if len(guide_board) > 1:
                max_size = 0
                
                for gb in guide_board:
                    size = self.box_size(gb['box'])
                    if max_size < size:
                        max_size = size
                        board = gb
            
            # 화살표가 모두 안내판 안에 있는 화살표인지 확인.
            ### --- xyxy xywh 확인 --- ###
            filtered_arrow = []
            for arrow in arrows:
                arrow_cen_x, arrow_cen_y = self.get_text_center(arrow['box'], 'yolo')
                x1, y1, x2, y2 = board['box']
                if (x1 <= arrow_cen_x <= x2) and (y1 <= arrow_cen_y <= y2):
                    filtered_arrow.append(arrow)
                
            print(f"[Filtering] 총 {len(filtered_arrow)}개의 화살표 인식")
            return board, filtered_arrow, toilet
        
        except:
            print('[Filtering] 인덱싱 에러 발생.')
            return False, False, False
        
        
    
    def mapping_arrow(self, data, arrow_data,feat):
        result = {'arrows':[]}
        if feat == 'guide':
            
            for text in data:
                text_point = text['center']
                direction = ''
                min_distance = 9999
                for arrow in arrow_data:
                    arr_point = self.get_text_center(arrow['box'], 'yolo')
                    dist = self.distance(text_point, arr_point)
                    
                    if dist < min_distance:
                        min_distance = dist
                        direction = arrow['label']
                result['arrows'].append({"type": self.label_onboarding[direction],
                                        "text": text['text']})
        
        elif feat == 'toilet':
            
            direction = ''
            min_distance = 9999
            for arrow in arrow_data:
                arr_point = self.get_text_center(arrow['box'], 'yolo')
                toilet_point = self.get_text_center(data[0]['box'], 'yolo')
                dist = self.distance(toilet_point, arr_point)
                
                if dist < min_distance:
                    min_distance = dist
                    direction = arrow['label']
            
            result = self.label_toilet[direction]
            
        return result
            
    def board(self,):
        
        box_info = self.box_info
        org_img = self.img
        # 화살표 있는지 체크하고 안내판, 화살표 리스트를 나눠서 받기.
        ## 화살표가 안내판 박스 안에 있는지 체크
        ### 해당 조건에 맞는 안내판 및 화살표 리스트 받아오기
        guide_board, arrow, _= self.filtering_object(box_info)
        
        if not guide_board:
            # crop한 안내판 이미지에 대해 OCR 진행
            crop_g_board, trans_arrow_box = self.crop_boxes(org_img, guide_board, arrow)        
            ocr_data = self.ocr(crop_g_board)
            for data in ocr_data:
                text = data['text']
                ## 텍스트 박스에 대해서 역 이름, 나가는 곳 등과 같은 정보만 필터링 및 단어 유사도 보완
                jc1, jc2 = jaccard_similarity(text)
                lv = Levenshtein_similarity(text)
                mod_text = find_same_string(jc1, jc2, lv)
                # --- 추가할 점 --- #
                ## 유사도 엄청 낮으면 아예 빼버려야함. ## 
                # 수정한 텍스트 반영
                data['text'] = mod_text
                if text != mod_text:
                    print(f'[텍스트 수정] 기존: {text} -> 수정: {mod_text}')
            print('글자 수정 완료')
            
            # 화살표 박스와 텍스트 박스의 유클리드 거리를 비교하여 맵핑
            result = self.mapping_arrow(ocr_data, trans_arrow_box,'guide')
        
        else:
            result=False
            
        return result
    
    def toilet(self,):
        
        box_info = self.box_info
        ## old
        # guide_board, arrow, toilet= self.filtering_object(box_info)
        ## NEW
        toilet = []
        arrows = []
        for box in box_info:
            if box['label'] == 3: # toilet
                toilet.append(box)
            elif box['label'] in [0,1,2,5]: # L, R, S, under    
                arrows.append(box)
        
        result = self.mapping_arrow(toilet, arrows,'toilet')

        return result
    
    def extract_number(self, input_string):
        current_num = ''
        for char in input_string:
            if char.isdigit():
                current_num += char
            elif char.isalpha():
                break

        if current_num:
            return int(current_num)
        else:
            return None
        
    def exit(self, exit_num):
        box_info = self.box_info
        org_img = self.img
            
        guide_board, arrow, _ = self.filtering_object(box_info)
        
        # crop한 안내판 이미지에 대해 OCR 진행
        crop_g_board, trans_arrow_box = self.crop_boxes(org_img, guide_board, arrow)        
        ocr_data = self.ocr(crop_g_board)
        check = False
        direction = ''
        num_lst = []
        for data in ocr_data:
            if data['text'] in ['나가는', '나가는곳', '나가', '곳', '나가는 곳', '출구', 'Exit']:
                check = True
                print(f'[EXIT {check}] 안내판에서 출구 문구 확인')
            if any(char.isdigit() for char in data['text']):
                num_lst.append(data)
        
        num_check = False
        if check:
            for num in num_lst:
                if '~' in num['text']:
                    matches = re.findall(r'\d+|~', num['text'])
                    numbers = [int(match) if match.isdigit() else None for match in matches]
                    numbers = [num for num in numbers if num is not None]

                    if numbers[0] <= exit_num and numbers[1] >= exit_num:
                        num_check = True
                        break
                else:
                    pure_num = str(self.extract_number(num['text']))
                    if str(exit_num) in pure_num:
                        num_check = True
                        break
        
        if num_check:
            big_arrow = trans_arrow_box[0]
            if len(trans_arrow_box) > 1:
                max_size = 0
                
                for ab in trans_arrow_box:
                    size = self.box_size(ab['box'])
                    if max_size < size:
                        max_size = size
                        big_arrow = ab
                
            direction = big_arrow['label']
            
        return self.label_onboarding[direction]
            
            
    def start(self,):
        s = time.time()
        # 상황에 따라 세가지로 나눠서 처리
        ## 1. 길찾기
        ## 2. 화장실찾기
        ## 3. 출구찾기
        if self.situation == 'board':
            print('[Start] 승강장 찾기 시작.')
            res = self.board()
        elif self.situation == 'toilet':
            print('[Start] 화장실 찾기 시작.')
            res = self.toilet()
        elif self.situation == 'exit':
            print('[Start] 출구 찾기 시작.')
            res = self.exit('2')
        e = time.time()
        print(f'총 {e-s}초 소요')
        return res


if __name__ == "__main__":
    
    image_path = '/Users/yongcho/dev/yonggit/eyeway_ai/-_image71_jpg.rf.d350ba82407871a16db65297270abc91.jpg'
    with open(image_path, 'rb') as image_file:
        image_data = image_file.read()
        
    box = [{'label':0, 'box':[0, 422, 616, 514]},
           {'label':2, 'box':[567 ,435 ,589 ,471]}]
    
    encoded_image = base64.b64encode(image_data).decode('utf-8')
    # encoded_image = 
    guide = Guide(encoded_image, box, situation='exit')
    res = guide.start()
    print(res)