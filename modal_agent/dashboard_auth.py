"""Dashboard authentication, remembered devices and mutation protection."""
import hashlib
import hmac
import os
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from core.auth import COOKIE, MAX_AGE, issue, valid

SESSION = 'assistant_session'


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(max_length=1024)
    remember: bool = True


class DashboardAuth:
    def __init__(self, password=None, *, secure_cookies=True, preview=False):
        self.password = password
        self.secure_cookies = secure_cookies
        self.preview = preview
        self.attempts = {}
        self.lock = threading.Lock()
        self.router = APIRouter()
        self.router.add_api_route('/login', self.login, methods=['POST'])
        self.router.add_api_route('/session', self.session, methods=['GET'])
        self.router.add_api_route('/logout', self.logout, methods=['POST'],
                                  dependencies=[Depends(self.mutation)])

    def secret(self):
        value = self.password if self.password is not None else os.environ.get('DASHBOARD_PASSWORD')
        if not value:
            raise HTTPException(503, 'Dashboard access is unavailable. The owner must configure it.')
        return value

    def credential(self, request: Request):
        expected = self.secret()
        token = request.cookies.get(SESSION)
        if valid(token, expected + ':dashboard-session'):
            return token
        token = request.cookies.get(COOKIE)
        if valid(token, expected):
            return token
        raise HTTPException(401, 'Please sign in to your workspace.')

    def csrf(self, token):
        return hmac.new(self.secret().encode(), ('dashboard-csrf:' + token).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def origin(request):
        if request.headers.get('origin') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'This action must come from your dashboard.')

    def mutation(self, request: Request):
        token = self.credential(request)
        self.origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), self.csrf(token)):
            raise HTTPException(403, 'Refresh the dashboard before trying this action again.')

    def cookie(self, response, name, token, remember=False):
        response.set_cookie(name, token, max_age=MAX_AGE if remember else None,
                            secure=self.secure_cookies, httponly=True, samesite='strict', path='/')

    def login(self, body: Login, request: Request, response: Response):
        self.origin(request)
        expected = self.secret()
        client = request.client.host if request.client else 'unknown'
        with self.lock:
            now = time.monotonic()
            for key in list(self.attempts):
                if now - self.attempts[key][1] > 60:
                    self.attempts.pop(key)
            count, started = self.attempts.get(client, (0, now))
            if count >= 5:
                raise HTTPException(429, 'Too many attempts. Please wait a minute.')
            if not hmac.compare_digest(body.password.encode(), expected.encode()):
                if len(self.attempts) >= 1000 and client not in self.attempts:
                    raise HTTPException(429, 'Please wait a minute before trying again.')
                self.attempts[client] = (count + 1, started)
                raise HTTPException(401, 'Password not recognised.')
            self.attempts.pop(client, None)
        session = issue(expected + ':dashboard-session')
        self.cookie(response, SESSION, session)
        if body.remember:
            self.cookie(response, COOKIE, issue(expected), remember=True)
        else:
            response.delete_cookie(COOKIE, path='/', secure=self.secure_cookies,
                                   httponly=True, samesite='strict')
        return {'csrf': self.csrf(session), 'preview': self.preview}

    def session(self, request: Request):
        return {'csrf': self.csrf(self.credential(request)), 'preview': self.preview}

    def logout(self, response: Response):
        for name in (SESSION, COOKIE):
            response.delete_cookie(name, path='/', secure=self.secure_cookies,
                                   httponly=True, samesite='strict')
        return {'message': 'Signed out.'}
