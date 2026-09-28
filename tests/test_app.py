import unittest
from unittest.mock import patch
import app as server


class AttendanceTests(unittest.TestCase):
    def setUp(self):
        server.ledger = server.Ledger()
        server.student_tokens = {}
        server.students = {'14': {'student_id': '14', 'name': 'Soham Dharap'}}
        server.active_class = server.new_class('Lecture')
        self.teacher = server.app.test_client()
        self.student = server.app.test_client()
        self.guest = server.app.test_client()
        self.post(self.teacher, '/api/login/teacher', username='Payal Mam', password='BT#PT')
        self.post(self.student, '/api/login/student', username='SOHAMdharap', password='14')

    def post(self, client, path, **data):
        return client.post(path, json=data, headers={'X-Requested-With': 'Provex'})

    def token(self):
        return self.student.get('/api/student/qr').json['token']

    def scan(self, token=None):
        return self.post(self.teacher, '/api/scan', token=token or self.token())

    def test_health_and_pages(self):
        self.assertEqual(self.guest.get('/healthz').json, {'status': 'ok'})
        self.assertEqual(self.guest.get('/').status_code, 302)
        self.assertIn(b'Teacher sign in', self.guest.get('/admin/dashboard').data)
        self.assertIn(b'Your classroom', self.teacher.get('/admin/dashboard').data)
        self.assertIn(b'Soham Dharap', self.student.get('/student').data)

    def test_camera_only_scanner_markup(self):
        page = self.teacher.get("/admin/dashboard").data
        self.assertIn(b'id="start-camera"', page)
        self.assertIn(b'id="scan-confirmation"', page)
        self.assertIn(b'id="scan-next"', page)
        self.assertNotIn(b'id="qr-file"', page)
        self.assertNotIn(b'id="scan-token"', page)
        self.assertNotIn(b'type="file"', page)

    def test_teacher_and_student_authorization(self):
        for client in (self.student, self.guest):
            for path in ('/api/ledger', '/api/students', '/api/class'):
                self.assertEqual(client.get(path).status_code, 401)
            for path in ('/api/scan', '/api/mine', '/api/class', '/api/students'):
                self.assertEqual(self.post(client, path).status_code, 401)
        self.assertEqual(self.teacher.get('/api/student/qr').status_code, 401)
        self.assertEqual(self.guest.get('/api/student/qr.svg').status_code, 401)

    def test_login_normalization(self):
        for name in ('sohamdharap', ' SOHAM   DHARAP ', 'sOhAm DhArAp'):
            response = self.post(self.guest, '/api/login/student', username=name, password='014')
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(self.guest, '/api/login/student', username='Soham Dharap', password='15').status_code, 401)
        self.assertEqual(self.post(self.guest, '/api/login/student', username='Soham Darap', password='14').status_code, 401)
        self.assertEqual(self.post(self.guest, '/api/login/student', username=[], password=14).status_code, 401)
        self.assertEqual(self.post(self.guest, '/api/login/teacher', username='Payal Mam', password='wrong').status_code, 401)

    def test_qr_unique_and_expires_at_sixty_seconds(self):
        import time
        now = time.time()
        with patch('app.time.time', return_value=now):
            token = self.token()
            data = self.student.get('/api/student/qr').json
            self.assertEqual(data['expires_at'] - data['server_time'], 60)
            image = self.student.get('/api/student/qr.svg', query_string={'token': token})
            self.assertEqual(image.status_code, 200)
            self.assertIn(b'<svg', image.data)
            self.assertEqual(image.headers['Cache-Control'], 'no-store')
        with patch('app.time.time', return_value=now + 59):
            self.assertEqual(self.scan(token).status_code, 200)
        with patch('app.time.time', return_value=now + 60):
            self.assertEqual(self.scan(token).status_code, 400)
            self.assertNotEqual(self.token(), token)

    def test_duplicate_after_sealing_and_new_practical(self):
        self.assertEqual(self.scan().status_code, 200)
        self.assertTrue(self.student.get('/api/student/status').json['present'])
        self.assertEqual(self.scan().status_code, 409)
        self.assertEqual(self.post(self.teacher, '/api/mine').status_code, 200)
        self.assertEqual(self.scan().status_code, 409)
        self.post(self.teacher, '/api/class', type='Practical')
        self.assertFalse(self.student.get('/api/student/status').json['present'])
        self.assertEqual(self.scan().status_code, 200)
        data = self.teacher.get('/api/ledger').json
        self.assertEqual(data['pending'][0]['session_type'], 'Practical')
        self.assertEqual(data['chain'][1]['transactions'][0]['session_type'], 'Lecture')
        self.assertEqual(data['pending'][0]['subject'], 'Blockchain & Technology')
        self.assertTrue(data['integrity_valid'])

    def test_roster_creation_and_login(self):
        response = self.post(self.teacher, '/api/students', student_id='15', name=' Shravani  Dongre ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(self.teacher, '/api/students', student_id='15', name='Other').status_code, 409)
        for roll in ('0', '-1', 'a', '123456789', [], None):
            self.assertEqual(self.post(self.teacher, '/api/students', student_id=roll, name='Test').status_code, 400)
        response = self.post(self.guest, '/api/login/student', username='shravaniDongre', password='15')
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.token(), self.guest.get('/api/student/qr').json['token'])

    def test_rejects_forged_or_missing_qr(self):
        for token in ('forged', {}, None, 'a' * 1025):
            self.assertEqual(self.post(self.teacher, '/api/scan', token=token).status_code, 400)
        token = self.token()
        self.assertEqual(self.scan(token + 'tampered').status_code, 400)
        self.assertEqual(self.teacher.post('/api/scan', json={'token': token}).status_code, 403)
        self.assertEqual(self.post(self.teacher, '/api/class', type='Invalid').status_code, 400)

    def test_student_cannot_read_another_qr(self):
        token = self.token()
        self.post(self.teacher, '/api/students', student_id='15', name='Shravani Dongre')
        self.post(self.guest, '/api/login/student', username='Shravani Dongre', password='15')
        response = self.guest.get('/api/student/qr.svg', query_string={'token': token})
        self.assertEqual(response.status_code, 400)

    def test_integrity_and_empty_mine(self):
        self.assertEqual(self.post(self.teacher, '/api/mine').status_code, 400)
        self.scan()
        self.post(self.teacher, '/api/mine')
        server.ledger.chain[1].transactions[0]['name'] = 'Changed'
        self.assertFalse(self.teacher.get('/api/ledger').json['integrity_valid'])

    def test_logout_and_role_switch(self):
        self.assertEqual(self.post(self.teacher, '/api/logout').status_code, 200)
        self.assertEqual(self.teacher.get('/api/ledger').status_code, 401)
        self.post(self.teacher, '/api/login/teacher', username='Payal Mam', password='BT#PT')
        self.post(self.teacher, '/api/login/student', username='Soham Dharap', password='14')
        self.assertEqual(self.teacher.get('/api/ledger').status_code, 401)


if __name__ == '__main__':
    unittest.main()
