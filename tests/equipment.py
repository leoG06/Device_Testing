import pyvisa
import datetime
import numpy as np


def available_connections(rm=None):
    """ Function that return the list of resource connected to the computer """
    if rm==None:
        rm = pyvisa.ResourceManager()    
    return rm.list_resources()

def resource_manager():
    """ Function that return a pyvisa resource manager to deal with resources connected to the computer """

    rm = pyvisa.ResourceManager()
    return rm

class equipment():

    """ Parent class for all the equipment classes."""
    
        
    def __init__(self,address,rm=None,timeout=10e3):
        """ Function called when an instance of the class is created
        
            args:
                - address (string) : the address of the equipment to be connected
                - rm (pyvisa object) : the pyvisa resource manager used to set the communication with the equipment
                - timeout (scalar) : the timeout set for the communication with the equipment
        
        """

        if rm==None:
            rm = pyvisa.ResourceManager()
        self.pyvisa_resource = rm.open_resource(address)
        self.set_connection_parameter("timeout",timeout)
        
    def set_connection_parameter(self,key,value):
        """ Function to change a parameter of the communication with the equipment
        
            args:
                - key (string) : the parameter to be modifier 
                - value (scalar) : the value for the parameter
        
        """
        key_list=["timeout","write_termination","read_termination","send_end","baud_rate"]
        if key=="timeout":
            self.pyvisa_resource.timeout=value
        elif key=="write_termination":
            self.pyvisa_resource.write_termination=value
        elif key=="read_termination":
            self.pyvisa_resource.read_termination=value                    
        elif key=="send_end":
            self.pyvisa_resource.send_end=value
        elif key=="baud_rate":
            self.pyvisa_resource.baud_rate =value
        else:
            print("Parameter not valid. Valid parameter are %s"%key_list)
        
            
    def set_connection_parameter_dic(self,connection_parameter):
        """ Function to change a parameter of the communication with the equipment
        
            args:
                - connection_parameter (dictionary) = dictionary with key and value to be sent to function set_connection_parameter(key,value)
        
        """
        if isinstance(connection_parameter, dict):
            for key, value in connection_parameter.items():
                self.set_connection_parameter(key,value)
        else:
            print("Please provide a dictionnary as argument.")
        
        
    def close_connection(self):
        """ Function to close the connection with an equipment """
        self.pyvisa_resource.close()

    def write_in_file(self,file_path,data,delimiter=",",overwrite=False,header=None,date=True, comment="#"):
        """ Function to write data in a file
        
            args:
                - file_path (string) : path for the data file, including the filename with its extension
                - data (scalar, list or array) : the data to be written in the file
                - delimiter (char) : the delimiter to separate the column of the data
                - overwrite (boolean) : if True overwrite the existing file if any, if False, append the data to the existing file if any
                - header (string) : header to be written before the data
                - date (boolean) : date to be written at the beginning of the file
                - comment (char) : char to be written before the header and date to indicate non-data lines
        
        """        
        if file_path.split(".")[-1]=="csv":
            delimiter=","
            
        # Create file and header
        if overwrite:
            f = open(file_path, "w")
        else:
            f = open(file_path, "a")
            
        if date:
            f.write("%s %s\n"%(comment,datetime.datetime.now().strftime("%c")))
        
        if isinstance(header, str):
            for line in header.split("\n"):
                f.write(comment+" "+line+"\n")

        
        shape=np.shape(data)
        if len(shape)==0:
            f.write("%.6E\n"%(data))
        elif len(shape)==1:
            for i in range(shape[0]):
                if i==0:
                    f.write("%.6E"%(data[i]))
                else:
                    f.write("%s%.6E"%(delimiter,data[i]))

            f.write("\n")

        elif len(shape)==2:
            for i in range(shape[0]):
                for j in range(shape[1]):
                    if j==0:
                        f.write("%.6E"%(data[i,j]))
                    else:
                        f.write("%s%.6E"%(delimiter,data[i,j]))
                f.write("\n")                
        f.close()
